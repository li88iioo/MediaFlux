from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading
import unittest

from app import database as db
from app.modules.telegram_write_confirmations import (
    SQLiteTelegramWriteConfirmationStore,
    TelegramWriteConfirmationError,
    TelegramWriteConfirmationStore,
)
from tests.support import IsolatedDatabaseTestCase


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

    def test_new_group_replaces_previous_owner_confirmation(self):
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


class SQLiteTelegramWriteConfirmationStoreTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM agent_confirmations")
            conn.execute("DELETE FROM agent_confirmation_epochs")

    def test_pair_persists_owner_hashed_and_consumes_across_instances(self):
        tokens = iter(["confirm_ticket_000001"])
        first = SQLiteTelegramWriteConfirmationStore(
            token_factory=lambda: next(tokens)
        )
        confirm, cancel = first.create_pair(
            chat_id="100",
            user_id="9",
            operation="rss_refresh",
            value={"subscription_id": 7},
        )
        with db.get_conn() as conn:
            owner_digest = conn.execute(
                "SELECT owner_digest FROM agent_confirmations "
                "WHERE confirmation_id=?",
                (confirm.rsplit(".", 1)[0],),
            ).fetchone()["owner_digest"]
        self.assertNotIn("100", str(owner_digest))
        self.assertRegex(str(owner_digest), r"^[0-9a-f]{64}$")

        second = SQLiteTelegramWriteConfirmationStore()
        with self.assertRaisesRegex(TelegramWriteConfirmationError, "不属于"):
            second.claim(confirm, chat_id="100", user_id="10")
        self.assertEqual(
            second.claim(confirm, chat_id="100", user_id="9"),
            {
                "decision": "confirm",
                "operation": "rss_refresh",
                "value": {"subscription_id": 7},
            },
        )
        with self.assertRaisesRegex(TelegramWriteConfirmationError, "已处理"):
            first.claim(cancel, chat_id="100", user_id="9")

    def test_group_claim_is_atomic_across_store_instances(self):
        tokens = iter(["group_choice_ticket_0001"])
        action_ids = SQLiteTelegramWriteConfirmationStore(
            token_factory=lambda: next(tokens)
        ).create_group(
            chat_id="-100",
            user_id="9",
            operation="download_request",
            actions=[
                ("confirm", {"target": "qb"}),
                ("confirm", {"target": "guangya"}),
            ],
        )
        barrier = threading.Barrier(2)

        def claim_once(action_id: str) -> str:
            barrier.wait(timeout=3)
            try:
                SQLiteTelegramWriteConfirmationStore().claim(
                    action_id, chat_id="-100", user_id="9"
                )
            except TelegramWriteConfirmationError:
                return "invalid"
            return "claimed"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = sorted(pool.map(claim_once, action_ids))
        self.assertEqual(outcomes, ["claimed", "invalid"])


if __name__ == "__main__":
    unittest.main()
