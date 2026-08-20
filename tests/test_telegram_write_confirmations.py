from __future__ import annotations

import unittest

from app.modules.telegram_write_confirmations import (
    TelegramWriteConfirmationError,
    TelegramWriteConfirmationStore,
)


class TelegramWriteConfirmationStoreTests(unittest.TestCase):
    def test_confirmation_is_owner_bound_and_wrong_owner_does_not_consume_it(self):
        store = TelegramWriteConfirmationStore()
        confirm_id, _cancel_id = store.create_pair(
            chat_id="100",
            user_id="9",
            operation="rss_refresh",
            value={"subscription_id": 7},
        )
        with self.assertRaisesRegex(TelegramWriteConfirmationError, "不属于"):
            store.claim(confirm_id, chat_id="100", user_id="10")
        action = store.claim(confirm_id, chat_id="100", user_id="9")
        self.assertEqual(action, {
            "decision": "confirm",
            "operation": "rss_refresh",
            "value": {"subscription_id": 7},
        })

    def test_claim_is_single_use_and_invalidates_paired_decision(self):
        store = TelegramWriteConfirmationStore()
        confirm_id, cancel_id = store.create_pair(
            chat_id="100",
            user_id="9",
            operation="rss_download",
            value={"entry_id": 3},
        )
        self.assertEqual(
            store.claim(cancel_id, chat_id="100", user_id="9")["decision"],
            "cancel",
        )
        for action_id in (confirm_id, cancel_id):
            with self.assertRaisesRegex(TelegramWriteConfirmationError, "已处理"):
                store.claim(action_id, chat_id="100", user_id="9")

    def test_expired_confirmation_is_rejected(self):
        now = [10.0]
        store = TelegramWriteConfirmationStore(
            ttl_seconds=30,
            clock=lambda: now[0],
        )
        confirm_id, _cancel_id = store.create_pair(
            chat_id="100",
            user_id="9",
            operation="resource_download",
            value={"result_id": "r1", "target": "qb"},
        )
        now[0] = 40.0
        with self.assertRaisesRegex(TelegramWriteConfirmationError, "过期"):
            store.claim(confirm_id, chat_id="100", user_id="9")

    def test_group_is_owner_bound_and_any_choice_invalidates_all_siblings(self):
        store = TelegramWriteConfirmationStore()
        action_ids = store.create_group(
            chat_id="-100",
            user_id="9",
            operation="download_request",
            actions=[
                ("confirm", {"request_id": 7, "target": "qb"}),
                ("confirm", {"request_id": 7, "target": "guangya"}),
                ("cancel", {"request_id": 7}),
            ],
        )
        with self.assertRaisesRegex(TelegramWriteConfirmationError, "不属于"):
            store.claim(action_ids[0], chat_id="-100", user_id="10")

        action = store.claim(action_ids[1], chat_id="-100", user_id="9")
        self.assertEqual(action["value"]["target"], "guangya")
        for action_id in action_ids:
            with self.assertRaisesRegex(TelegramWriteConfirmationError, "已处理"):
                store.claim(action_id, chat_id="-100", user_id="9")

    def test_capacity_evicts_whole_oldest_group(self):
        store = TelegramWriteConfirmationStore(max_actions=4)
        old_ids = store.create_group(
            chat_id="100",
            user_id="9",
            operation="old",
            actions=[("confirm", {}), ("cancel", {})],
        )
        new_ids = store.create_group(
            chat_id="100",
            user_id="9",
            operation="new",
            actions=[("confirm", {}), ("confirm", {}), ("cancel", {})],
        )
        for action_id in old_ids:
            with self.assertRaisesRegex(TelegramWriteConfirmationError, "已处理"):
                store.claim(action_id, chat_id="100", user_id="9")
        self.assertEqual(
            store.claim(new_ids[0], chat_id="100", user_id="9")["operation"],
            "new",
        )


if __name__ == "__main__":
    unittest.main()
