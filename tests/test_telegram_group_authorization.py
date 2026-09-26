from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch


class TelegramGroupWriteAuthorizationTests(unittest.TestCase):
    @staticmethod
    def _message(*, chat_id: int, user_id: int, text: str = ""):
        return SimpleNamespace(
            chat=SimpleNamespace(id=chat_id),
            from_user=SimpleNamespace(id=user_id),
            text=text,
            message_id=17,
        )

    @staticmethod
    def _call(*, prefix: str, chat_id: int = -100, user_id: int = 10):
        return SimpleNamespace(
            id="callback",
            data=f"{prefix}opaque",
            from_user=SimpleNamespace(id=user_id),
            message=SimpleNamespace(
                chat=SimpleNamespace(id=chat_id),
                message_id=23,
            ),
        )

    @staticmethod
    def _values(group: bool = True):
        return {
            "TG_CHAT_ID": "-100" if group else "100",
            "TG_AGENT_ALLOWED_USER_IDS": "9",
        }

    @staticmethod
    def _patch_values(values):
        return (
            patch(
                "app.bot.handlers.get",
                side_effect=lambda key, default="": values.get(key, default),
            ),
            patch(
                "app.bot.agent_adapter.config.get",
                side_effect=lambda key, default="": values.get(key, default),
            ),
        )

    def test_group_write_guard_requires_allowed_user_but_private_chat_stays_compatible(
        self,
    ):
        from app.bot import handlers
        from tests.test_production import TelegramBotTests

        class Bot(TelegramBotTests.FakeBot):
            def __init__(self):
                super().__init__()
                self.answers = []

            def answer_callback_query(self, *args, **kwargs):
                self.answers.append((args, kwargs))

        bot = Bot()
        first, second = self._patch_values(self._values())
        with first, second:
            self.assertTrue(
                handlers._reject_unauthorized_group_write(
                    bot, self._message(chat_id=-100, user_id=10)
                )
            )
            self.assertEqual(bot.replies[-1][1], "你无权在此群组执行该操作")
            self.assertFalse(
                handlers._reject_unauthorized_group_write(
                    bot, self._message(chat_id=-100, user_id=9)
                )
            )

        private_values = self._values(group=False)
        with patch(
            "app.bot.handlers.get",
            side_effect=lambda key, default="": private_values.get(key, default),
        ):
            self.assertFalse(
                handlers._reject_unauthorized_group_write(
                    bot, self._message(chat_id=100, user_id=999)
                )
            )

    def test_group_member_cannot_invoke_legacy_write_commands_or_torrent_handler(self):
        from app.bot import handlers
        from tests.test_production import TelegramBotTests

        bot = TelegramBotTests.FakeBot()
        telebot = TelegramBotTests._telebot_types()
        first, second = self._patch_values(self._values())
        with first, second:
            handlers._register_commands(bot, telebot)
            command_text = {
                "sync_gy": "/sync_gy",
                "organize": "/organize",
                "rss_refresh": "/rss_refresh 1",
                "rss_dl": "/rss_dl 1",
            }
            for command, text in command_text.items():
                handler = next(
                    registered
                    for filters, registered in bot.message_handlers
                    if filters.get("commands") == [command]
                )
                handler(self._message(chat_id=-100, user_id=10, text=text))

            document_handler = next(
                registered
                for filters, registered in bot.message_handlers
                if filters.get("content_types") == ["document"]
            )
            document_message = self._message(chat_id=-100, user_id=10)
            document_message.document = SimpleNamespace(
                file_name="unsafe.torrent",
                mime_type="application/x-bittorrent",
                file_id="file",
            )
            document_handler(document_message)

        denied = [reply[1] for reply in bot.replies]
        self.assertEqual(denied, ["你无权在此群组执行该操作"] * 5)

    def test_group_member_cannot_submit_plain_download_link(self):
        from app.bot import handlers
        from tests.test_production import TelegramBotTests

        bot = TelegramBotTests.FakeBot()
        telebot = TelegramBotTests._telebot_types()
        first, second = self._patch_values(self._values())
        with (
            first,
            second,
            patch("app.modules.download_dispatcher.create_request") as create_request,
        ):
            handlers._register_commands(bot, telebot)
            receive_link = next(
                registered
                for filters, registered in bot.message_handlers
                if filters.get("content_types") == ["text"]
                and filters.get("func") is not None
                and filters["func"](
                    self._message(
                        chat_id=-100,
                        user_id=10,
                        text="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567",
                    )
                )
            )
            receive_link(
                self._message(
                    chat_id=-100,
                    user_id=10,
                    text="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567",
                )
            )

        create_request.assert_not_called()
        self.assertEqual(bot.replies[-1][1], "你无权在此群组执行该操作")

    def test_plain_web_page_is_not_created_as_download_request(self):
        from app.bot import handlers
        from tests.test_production import TelegramBotTests

        bot = TelegramBotTests.FakeBot()
        telebot = TelegramBotTests._telebot_types()
        values = {"TG_CHAT_ID": "100", "TG_AGENT_ALLOWED_USER_IDS": ""}
        first, second = self._patch_values(values)
        message = self._message(
            chat_id=100,
            user_id=9,
            text="http://192.168.0.195:1258/guangya/offline",
        )
        with (
            first,
            second,
            patch("app.modules.download_dispatcher.create_request") as create_request,
            patch(
                "app.bot.agent_adapter.handle_agent_message",
                return_value=False,
            ) as handle_agent_message,
        ):
            handlers._register_commands(bot, telebot)
            receive_link = next(
                registered
                for filters, registered in bot.message_handlers
                if filters.get("content_types") == ["text"]
                and filters.get("func") is not None
                and filters["func"](message)
            )
            receive_link(message)
            from app.bot.agent_adapter import AGENT_EXECUTOR
            self.assertTrue(AGENT_EXECUTOR.stop(timeout=2, cancel_queries=False))

        create_request.assert_not_called()
        handle_agent_message.assert_called_once_with(bot, telebot, message)

    def test_stale_plain_web_picker_cannot_dispatch_after_routing_fix(self):
        from app.bot import handlers
        from tests.test_production import TelegramBotTests

        class Bot(TelegramBotTests.FakeBot):
            def __init__(self):
                super().__init__()
                self.answers = []
                self.edits = []

            def answer_callback_query(self, *args, **kwargs):
                self.answers.append((args, kwargs))

            def edit_message_text(self, *args, **kwargs):
                self.edits.append((args, kwargs))

        bot = Bot()
        call = SimpleNamespace(
            id="stale-web-choice",
            data="tgc:opaque",
            from_user=SimpleNamespace(id=9),
            message=SimpleNamespace(
                chat=SimpleNamespace(id=100),
                message_id=23,
            ),
        )
        store = SimpleNamespace(
            claim=lambda *args, **kwargs: {
                "operation": "download_request",
                "decision": "confirm",
                "value": {"request_id": 77, "target": "guangya"},
            }
        )
        row = {
            "id": 77,
            "status": "pending",
            "kind": "http",
            "source_value": "http://192.168.0.195:1258/guangya/offline",
        }
        with (
            patch(
                "app.modules.telegram_write_confirmations.get_telegram_write_confirmation_store",
                return_value=store,
            ),
            patch(
                "app.bot.handlers.db.bind_pending_download_request_owner",
                return_value=row,
            ),
            patch(
                "app.bot.handlers.db.cancel_pending_download_request",
                return_value=True,
            ) as claim_request,
            patch(
                "app.bot.handlers.db.update_download_request",
            ) as update_request,
            patch(
                "app.bot.handlers._dispatch_download_callback",
            ) as dispatch,
        ):
            handlers._handle_write_confirmation_callback(bot, call, SimpleNamespace())

        dispatch.assert_not_called()
        claim_request.assert_called_once_with(77, error="普通网页链接未提交下载")
        update_request.assert_not_called()
        self.assertIn("普通网页不会创建下载任务", bot.answers[-1][0][1])
        self.assertIn("未提交下载", bot.edits[-1][0][0])

    def test_private_write_callback_keeps_legacy_chat_only_authorization(self):
        from app.bot import handlers
        from tests.test_production import TelegramBotTests

        bot = TelegramBotTests.FakeBot()
        telebot = TelegramBotTests._telebot_types()
        values = {"TG_CHAT_ID": "100", "TG_AGENT_ALLOWED_USER_IDS": ""}
        with (
            patch(
                "app.bot.handlers.get",
                side_effect=lambda key, default="": values.get(key, default),
            ),
            patch("app.bot.handlers._handle_share_callback") as share,
        ):
            handlers._register_commands(bot, telebot)
            callback = next(
                registered
                for filters, registered in bot.callback_handlers
                if filters["func"](SimpleNamespace(data="gys:opaque"))
            )
            callback(self._call(prefix="gys:", chat_id=100, user_id=999))

        share.assert_called_once()

    def test_group_member_cannot_use_legacy_write_callbacks(self):
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
        first, second = self._patch_values(self._values())
        with (
            first,
            second,
            patch("app.bot.handlers._handle_share_callback") as share,
            patch(
                "app.bot.handlers._handle_organize_confirmation_callback"
            ) as organize,
            patch("app.bot.handlers._dispatch_download_callback") as dispatch,
        ):
            handlers._register_commands(bot, telebot)
            for prefix in ("gys:", "orgc:", "dl:"):
                callback = next(
                    registered
                    for filters, registered in bot.callback_handlers
                    if filters["func"](SimpleNamespace(data=f"{prefix}opaque"))
                )
                callback(self._call(prefix=prefix))

        share.assert_not_called()
        organize.assert_not_called()
        dispatch.assert_not_called()
        self.assertEqual(len(bot.answers), 3)
        self.assertTrue(all(item[1]["show_alert"] for item in bot.answers))
        self.assertTrue(
            all(item[0][1] == "你无权在此群组执行该操作" for item in bot.answers)
        )

    def test_allowlisted_group_member_cannot_use_another_users_download_picker(self):
        from app.bot import handlers
        from app.modules.telegram_write_confirmations import (
            get_telegram_write_confirmation_store,
            reset_telegram_write_confirmation_store_for_tests,
        )
        from tests.test_production import TelegramBotTests

        class Bot(TelegramBotTests.FakeBot):
            def __init__(self):
                super().__init__()
                self.answers = []
                self.edits = []

            def answer_callback_query(self, *args, **kwargs):
                self.answers.append((args, kwargs))

            def edit_message_text(self, *args, **kwargs):
                self.edits.append((args, kwargs))

        reset_telegram_write_confirmation_store_for_tests()
        bot = Bot()
        telebot = TelegramBotTests._telebot_types()
        values = {
            "TG_CHAT_ID": "-100",
            "TG_AGENT_ALLOWED_USER_IDS": "9,10",
        }
        first, second = self._patch_values(values)
        with (
            first,
            second,
            patch(
                "app.modules.download_dispatcher.create_request",
                return_value={"created": True, "id": 77, "status": "pending"},
            ),
            patch(
                "app.bot.handlers._nsfw_download_sources",
                return_value=[{"id": "adult-a", "name": "成人来源 A"}],
            ),
        ):
            handlers._register_commands(bot, telebot)
            receive_link = next(
                registered
                for filters, registered in bot.message_handlers
                if filters.get("content_types") == ["text"]
                and filters.get("func") is not None
                and filters["func"](
                    self._message(
                        chat_id=-100,
                        user_id=9,
                        text="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567",
                    )
                )
            )
            receive_link(
                self._message(
                    chat_id=-100,
                    user_id=9,
                    text="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567",
                )
            )
            button = next(
                button
                for button in bot.replies[-1][2]["reply_markup"].buttons
                if button.text.startswith("光鸭·NSFW")
            )
            callback_data = button.callback_data
            self.assertTrue(callback_data.startswith("tgc:"))
            callback = next(
                registered
                for filters, registered in bot.callback_handlers
                if filters["func"](SimpleNamespace(data=callback_data))
            )
            callback(
                SimpleNamespace(
                    id="foreign-choice",
                    data=callback_data,
                    from_user=SimpleNamespace(id=10),
                    message=SimpleNamespace(
                        chat=SimpleNamespace(id=-100),
                        message_id=23,
                    ),
                )
            )

        self.assertIn("不属于", bot.answers[-1][0][1])
        self.assertEqual(bot.edits, [])
        action = get_telegram_write_confirmation_store().claim(
            callback_data[4:],
            chat_id="-100",
            user_id="9",
        )
        self.assertEqual(action["value"]["request_id"], 77)
        self.assertEqual(action["value"]["nsfw_source_id"], "adult-a")

    def test_duplicate_pending_link_reissues_owner_bound_confirmation_picker(self):
        from app.bot import handlers
        from app.modules.telegram_write_confirmations import (
            reset_telegram_write_confirmation_store_for_tests,
        )
        from tests.test_production import TelegramBotTests

        reset_telegram_write_confirmation_store_for_tests()
        bot = TelegramBotTests.FakeBot()
        telebot = TelegramBotTests._telebot_types()
        values = self._values(group=False)
        first, second = self._patch_values(values)
        persisted = {
            "id": 77,
            "status": "pending",
            "title": "已保存任务",
            "chat_id": "100",
            "user_id": "9",
        }
        with (
            first,
            second,
            patch(
                "app.modules.download_dispatcher.create_request",
                return_value={
                    "created": False,
                    "id": 77,
                    "status": "pending",
                    "title": "已保存任务",
                },
            ),
            patch(
                "app.bot.handlers.db.bind_pending_download_request_owner",
                return_value=persisted,
            ) as bind_owner,
        ):
            handlers._register_commands(bot, telebot)
            message = self._message(
                chat_id=100,
                user_id=9,
                text="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567",
            )
            receive_link = next(
                registered
                for filters, registered in bot.message_handlers
                if filters.get("content_types") == ["text"]
                and filters.get("func") is not None
                and filters["func"](message)
            )
            receive_link(message)

        bind_owner.assert_called_once_with(77, chat_id="100", user_id="9")
        self.assertIn("原确认已失效", bot.replies[-1][1])
        buttons = bot.replies[-1][2]["reply_markup"].buttons
        self.assertTrue(buttons)
        self.assertTrue(
            all(button.callback_data.startswith("tgc:") for button in buttons)
        )

    def test_legacy_download_callback_only_reissues_confirmation_and_never_dispatches(
        self,
    ):
        from app.bot import handlers
        from app.modules.telegram_write_confirmations import (
            reset_telegram_write_confirmation_store_for_tests,
        )
        from tests.test_production import TelegramBotTests

        class Bot(TelegramBotTests.FakeBot):
            def __init__(self):
                super().__init__()
                self.answers = []

            def answer_callback_query(self, *args, **kwargs):
                self.answers.append((args, kwargs))

        reset_telegram_write_confirmation_store_for_tests()
        bot = Bot()
        telebot = TelegramBotTests._telebot_types()
        first, second = self._patch_values(self._values(group=False))
        row = {
            "id": 77,
            "status": "pending",
            "title": "旧下载请求",
            "chat_id": "100",
            "user_id": "9",
        }
        with (
            first,
            second,
            patch(
                "app.bot.handlers.db.bind_pending_download_request_owner",
                return_value=row,
            ) as bind_owner,
            patch("app.bot.handlers._dispatch_download_callback") as dispatch,
        ):
            handlers._register_commands(bot, telebot)
            callback = next(
                registered
                for filters, registered in bot.callback_handlers
                if filters["func"](SimpleNamespace(data="dl:77:qb"))
            )
            callback(
                SimpleNamespace(
                    id="legacy-choice",
                    data="dl:77:qb",
                    from_user=SimpleNamespace(id=9),
                    message=SimpleNamespace(
                        chat=SimpleNamespace(id=100),
                        message_id=23,
                    ),
                )
            )

        dispatch.assert_not_called()
        bind_owner.assert_called_once_with(77, chat_id="100", user_id="9")
        self.assertIn("新的确认按钮", bot.answers[-1][0][1])
        self.assertIn("原确认已失效", bot.replies[-1][1])
        self.assertTrue(
            all(
                button.callback_data.startswith("tgc:")
                for button in bot.replies[-1][2]["reply_markup"].buttons
            )
        )

    def test_nsfw_picker_has_explicit_sources_and_ticket_dispatches_selected_source(
        self,
    ):
        from app.bot import handlers
        from app.modules.telegram_write_confirmations import (
            reset_telegram_write_confirmation_store_for_tests,
        )
        from tests.test_production import TelegramBotTests

        class Bot(TelegramBotTests.FakeBot):
            def __init__(self):
                super().__init__()
                self.answers = []
                self.edits = []

            def answer_callback_query(self, *args, **kwargs):
                self.answers.append((args, kwargs))

            def edit_message_text(self, *args, **kwargs):
                self.edits.append((args, kwargs))

        sources = [
            {"id": "adult-a", "name": "成人影视 A"},
            {"id": "adult-b", "name": "成人影视 B"},
        ]
        reset_telegram_write_confirmation_store_for_tests()
        bot = Bot()
        telebot = TelegramBotTests._telebot_types()
        with patch(
            "app.bot.handlers._nsfw_download_sources",
            return_value=sources,
        ):
            markup = handlers._download_target_picker_markup(
                telebot,
                request_id=77,
                chat_id="100",
                user_id="9",
            )
            nsfw_buttons = [
                button
                for button in markup.buttons
                if button.text.startswith("光鸭·NSFW")
            ]
            self.assertEqual(len(nsfw_buttons), 2)
            self.assertIn("成人影视 A", nsfw_buttons[0].text)
            self.assertIn("成人影视 B", nsfw_buttons[1].text)

            selected = nsfw_buttons[1]
            call = SimpleNamespace(
                id="nsfw-source-choice",
                data=selected.callback_data,
                from_user=SimpleNamespace(id=9),
                message=SimpleNamespace(
                    chat=SimpleNamespace(id=100),
                    message_id=23,
                ),
            )
            row = {
                "id": 77,
                "status": "pending",
                "kind": "magnet",
                "source_value": "magnet:?xt=urn:btih:fixture",
                "title": "待下载任务",
            }
            with (
                patch(
                    "app.bot.handlers.db.bind_pending_download_request_owner",
                    return_value=row,
                ) as bind_owner,
                patch("app.bot.handlers.threading.Thread") as thread,
            ):
                handlers._handle_write_confirmation_callback(bot, call, telebot)
                handlers._handle_write_confirmation_callback(bot, call, telebot)

        thread.assert_called_once()
        self.assertEqual(
            thread.call_args.kwargs["kwargs"],
            {"nsfw_source_id": "adult-b"},
        )
        bind_owner.assert_called_once_with(77, chat_id="100", user_id="9")
        self.assertTrue(bot.edits[0][1]["reply_markup"] is None)
        self.assertTrue(bot.answers[-1][1]["show_alert"])

    def test_single_nsfw_source_is_named_and_stored_in_ticket(self):
        from app.bot import handlers
        from app.modules.telegram_write_confirmations import (
            get_telegram_write_confirmation_store,
            reset_telegram_write_confirmation_store_for_tests,
        )
        from tests.test_production import TelegramBotTests

        reset_telegram_write_confirmation_store_for_tests()
        source = {"id": "adult-only", "name": "成人专区"}
        with patch(
            "app.bot.handlers._nsfw_download_sources",
            return_value=[source],
        ):
            markup = handlers._download_target_picker_markup(
                TelegramBotTests._telebot_types(),
                request_id=77,
                chat_id="100",
                user_id="9",
            )

        button = next(
            item for item in markup.buttons if item.text.startswith("光鸭·NSFW")
        )
        self.assertIn("成人专区", button.text)
        action = get_telegram_write_confirmation_store().claim(
            button.callback_data[4:],
            chat_id="100",
            user_id="9",
        )
        self.assertEqual(action["value"]["nsfw_source_id"], "adult-only")

    def test_ordinary_qb_picker_keeps_existing_dispatch_call_shape(self):
        from app.bot import handlers
        from app.modules.telegram_write_confirmations import (
            reset_telegram_write_confirmation_store_for_tests,
        )
        from tests.test_production import TelegramBotTests

        class Bot(TelegramBotTests.FakeBot):
            def answer_callback_query(self, *_args, **_kwargs):
                return None

            def edit_message_text(self, *_args, **_kwargs):
                return None

        reset_telegram_write_confirmation_store_for_tests()
        telebot = TelegramBotTests._telebot_types()
        row = {
            "id": 77,
            "status": "pending",
            "kind": "magnet",
            "source_value": "magnet:?xt=urn:btih:fixture",
            "title": "普通下载",
        }
        with patch(
            "app.bot.handlers._nsfw_download_sources",
            return_value=[],
        ):
            markup = handlers._download_target_picker_markup(
                telebot,
                request_id=77,
                chat_id="100",
                user_id="9",
            )
            qb_button = next(
                item for item in markup.buttons if item.text == "qBittorrent"
            )
            call = SimpleNamespace(
                id="ordinary-qb-choice",
                data=qb_button.callback_data,
                from_user=SimpleNamespace(id=9),
                message=SimpleNamespace(
                    chat=SimpleNamespace(id=100),
                    message_id=23,
                ),
            )
            with (
                patch(
                    "app.bot.handlers.db.bind_pending_download_request_owner",
                    return_value=row,
                ),
                patch("app.bot.handlers.threading.Thread") as thread,
            ):
                handlers._handle_write_confirmation_callback(
                    Bot(),
                    call,
                    telebot,
                )

        thread.assert_called_once()
        self.assertEqual(thread.call_args.kwargs["args"][-1], "qb")
        self.assertNotIn("kwargs", thread.call_args.kwargs)

    def test_empty_nsfw_choice_does_not_submit_and_keeps_other_targets_available(self):
        from app.bot import handlers
        from app.modules.telegram_write_confirmations import (
            reset_telegram_write_confirmation_store_for_tests,
        )
        from tests.test_production import TelegramBotTests

        class Bot(TelegramBotTests.FakeBot):
            def __init__(self):
                super().__init__()
                self.answers = []
                self.edits = []

            def answer_callback_query(self, *args, **kwargs):
                self.answers.append((args, kwargs))

            def edit_message_text(self, *args, **kwargs):
                self.edits.append((args, kwargs))

        reset_telegram_write_confirmation_store_for_tests()
        bot = Bot()
        telebot = TelegramBotTests._telebot_types()
        with patch(
            "app.bot.handlers._nsfw_download_sources",
            return_value=[],
        ):
            markup = handlers._download_target_picker_markup(
                telebot,
                request_id=77,
                chat_id="100",
                user_id="9",
            )
            button = next(
                button for button in markup.buttons if button.text == "光鸭·NSFW"
            )
            row = {
                "id": 77,
                "status": "pending",
                "kind": "magnet",
                "source_value": "magnet:?xt=urn:btih:fixture",
                "title": "待下载任务",
            }
            call = SimpleNamespace(
                id="empty-nsfw-choice",
                data=button.callback_data,
                from_user=SimpleNamespace(id=9),
                message=SimpleNamespace(
                    chat=SimpleNamespace(id=100),
                    message_id=23,
                ),
            )
            with (
                patch(
                    "app.bot.handlers.db.bind_pending_download_request_owner",
                    return_value=row,
                ),
                patch("app.bot.handlers.db.cancel_pending_download_request") as cancel,
                patch("app.bot.handlers.threading.Thread") as thread,
            ):
                handlers._handle_write_confirmation_callback(bot, call, telebot)

        thread.assert_not_called()
        cancel.assert_not_called()
        self.assertIn("未配置 NSFW", bot.answers[-1][0][1])
        replacement = bot.edits[-1][1]["reply_markup"]
        self.assertTrue(any(item.text == "qBittorrent" for item in replacement.buttons))
        self.assertTrue(any(item.text == "两者" for item in replacement.buttons))
        self.assertTrue(any(item.text == "光鸭·NSFW" for item in replacement.buttons))

    def test_stale_nsfw_choice_refreshes_picker_without_submitting(self):
        from app.bot import handlers
        from app.modules.telegram_write_confirmations import (
            reset_telegram_write_confirmation_store_for_tests,
        )
        from tests.test_production import TelegramBotTests

        class Bot(TelegramBotTests.FakeBot):
            def __init__(self):
                super().__init__()
                self.answers = []
                self.edits = []

            def answer_callback_query(self, *args, **kwargs):
                self.answers.append((args, kwargs))

            def edit_message_text(self, *args, **kwargs):
                self.edits.append((args, kwargs))

        reset_telegram_write_confirmation_store_for_tests()
        bot = Bot()
        telebot = TelegramBotTests._telebot_types()
        current_sources = [{"id": "adult-old", "name": "旧来源"}]
        with patch(
            "app.bot.handlers._nsfw_download_sources",
            side_effect=lambda: list(current_sources),
        ):
            markup = handlers._download_target_picker_markup(
                telebot,
                request_id=77,
                chat_id="100",
                user_id="9",
            )
            button = next(
                item for item in markup.buttons if item.text.startswith("光鸭·NSFW")
            )
            current_sources[:] = [{"id": "adult-new", "name": "新来源"}]
            row = {
                "id": 77,
                "status": "pending",
                "kind": "magnet",
                "source_value": "magnet:?xt=urn:btih:fixture",
                "title": "待下载任务",
            }
            call = SimpleNamespace(
                id="stale-nsfw-choice",
                data=button.callback_data,
                from_user=SimpleNamespace(id=9),
                message=SimpleNamespace(
                    chat=SimpleNamespace(id=100),
                    message_id=23,
                ),
            )
            with (
                patch(
                    "app.bot.handlers.db.bind_pending_download_request_owner",
                    return_value=row,
                ),
                patch("app.bot.handlers.threading.Thread") as thread,
            ):
                handlers._handle_write_confirmation_callback(bot, call, telebot)

        thread.assert_not_called()
        self.assertIn("来源已变更", bot.answers[-1][0][1])
        replacement = bot.edits[-1][1]["reply_markup"]
        labels = [item.text for item in replacement.buttons]
        self.assertTrue(any("新来源" in label for label in labels))
        self.assertFalse(any("旧来源" in label for label in labels))

    def test_nsfw_dispatch_revalidates_source_and_uses_existing_directory_target(self):
        from app.bot import handlers

        class Bot:
            def __init__(self):
                self.edits = []

            def edit_message_text(self, *args, **kwargs):
                self.edits.append((args, kwargs))

        bot = Bot()
        source = {"id": "adult-a", "name": "成人影视 A"}
        summary = {
            "status": "submitted",
            "succeeded": ["guangya"],
            "failed": [],
            "error": "",
        }
        tracker = SimpleNamespace(reload=lambda: None)
        with (
            patch(
                "app.bot.handlers._nsfw_download_sources",
                return_value=[source],
            ),
            patch(
                "app.modules.download_dispatcher.dispatch_request", return_value={}
            ) as dispatch,
            patch(
                "app.modules.download_dispatcher.public_dispatch_summary",
                return_value=summary,
            ),
            patch(
                "app.modules.download_tracker.get_download_tracker",
                return_value=tracker,
            ),
            patch(
                "app.bot.handlers._download_follow_up_text",
                return_value="现有后续提示",
            ),
        ):
            handlers._dispatch_download_callback(
                bot,
                100,
                23,
                77,
                "guangya",
                nsfw_source_id="adult-a",
            )

        dispatch.assert_called_once_with(
            77,
            "guangya",
            gy_target_dir="adult-a",
            gy_target_name="成人影视 A",
        )
        receipt = bot.edits[-1][0][0]
        self.assertIn("成人影视 A", receipt)
        self.assertIn("按 NSFW 规则整理", receipt)
        self.assertIn("现有后续提示", receipt)

        bot.edits.clear()
        with (
            patch(
                "app.bot.handlers._nsfw_download_sources",
                return_value=[],
            ),
            patch(
                "app.modules.download_dispatcher.dispatch_request"
            ) as stale_dispatch,
            patch(
                "app.modules.download_tracker.get_download_tracker"
            ) as tracker_getter,
        ):
            handlers._dispatch_download_callback(
                bot,
                100,
                23,
                78,
                "guangya",
                nsfw_source_id="adult-a",
            )

        stale_dispatch.assert_not_called()
        tracker_getter.assert_not_called()
        self.assertIn("本次未提交", bot.edits[-1][0][0])


if __name__ == "__main__":
    unittest.main()
