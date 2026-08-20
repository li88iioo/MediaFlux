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
                "app.bot.agent_adapter.get",
                side_effect=lambda key, default="": values.get(key, default),
            ),
        )

    def test_group_write_guard_requires_allowed_user_but_private_chat_stays_compatible(self):
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
                "organize_gy": "/organize_gy",
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
        self.assertEqual(denied, ["你无权在此群组执行该操作"] * 6)

    def test_group_member_cannot_submit_plain_download_link(self):
        from app.bot import handlers
        from tests.test_production import TelegramBotTests

        bot = TelegramBotTests.FakeBot()
        telebot = TelegramBotTests._telebot_types()
        first, second = self._patch_values(self._values())
        with first, second, patch(
            "app.modules.download_dispatcher.create_request"
        ) as create_request:
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

    def test_private_write_callback_keeps_legacy_chat_only_authorization(self):
        from app.bot import handlers
        from tests.test_production import TelegramBotTests

        bot = TelegramBotTests.FakeBot()
        telebot = TelegramBotTests._telebot_types()
        values = {"TG_CHAT_ID": "100", "TG_AGENT_ALLOWED_USER_IDS": ""}
        with patch(
            "app.bot.handlers.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch("app.bot.handlers._handle_share_callback") as share:
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
        with first, second, patch(
            "app.bot.handlers._handle_share_callback"
        ) as share, patch(
            "app.bot.handlers._handle_organize_confirmation_callback"
        ) as organize, patch(
            "app.bot.handlers._dispatch_download_callback"
        ) as dispatch:
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
        with first, second, patch(
            "app.modules.download_dispatcher.create_request",
            return_value={"created": True, "id": 77, "status": "pending"},
        ):
            handlers._register_commands(bot, telebot)
            receive_link = next(
                registered
                for filters, registered in bot.message_handlers
                if filters.get("content_types") == ["text"]
                and filters.get("func") is not None
                and filters["func"](self._message(
                    chat_id=-100,
                    user_id=9,
                    text="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567",
                ))
            )
            receive_link(self._message(
                chat_id=-100,
                user_id=9,
                text="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567",
            ))
            callback_data = bot.replies[-1][2]["reply_markup"].buttons[0].callback_data
            self.assertTrue(callback_data.startswith("tgc:"))
            callback = next(
                registered
                for filters, registered in bot.callback_handlers
                if filters["func"](SimpleNamespace(data=callback_data))
            )
            callback(SimpleNamespace(
                id="foreign-choice",
                data=callback_data,
                from_user=SimpleNamespace(id=10),
                message=SimpleNamespace(
                    chat=SimpleNamespace(id=-100),
                    message_id=23,
                ),
            ))

        self.assertIn("不属于", bot.answers[-1][0][1])
        self.assertEqual(bot.edits, [])
        action = get_telegram_write_confirmation_store().claim(
            callback_data[4:],
            chat_id="-100",
            user_id="9",
        )
        self.assertEqual(action["value"]["request_id"], 77)

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
        with first, second, patch(
            "app.modules.download_dispatcher.create_request",
            return_value={
                "created": False,
                "id": 77,
                "status": "pending",
                "title": "已保存任务",
            },
        ), patch(
            "app.bot.handlers.db.bind_pending_download_request_owner",
            return_value=persisted,
        ) as bind_owner:
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
        self.assertTrue(all(button.callback_data.startswith("tgc:") for button in buttons))

    def test_legacy_download_callback_only_reissues_confirmation_and_never_dispatches(self):
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
        with first, second, patch(
            "app.bot.handlers.db.bind_pending_download_request_owner",
            return_value=row,
        ) as bind_owner, patch(
            "app.bot.handlers._dispatch_download_callback"
        ) as dispatch:
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



if __name__ == "__main__":
    unittest.main()
