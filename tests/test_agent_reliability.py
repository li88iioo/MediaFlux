"""Agent 第10批：持久确认、链路追踪、异常脱敏与指标。"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

from app import database as db
from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.errors import AgentToolError
from app.agent.models import RiskLevel
from tests.support import IsolatedDatabaseTestCase


class SQLiteConfirmationStoreTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        SQLiteConfirmationStore().reset()

    def test_expected_owner_generation_rejects_bool_even_when_epoch_is_one(self):
        store = SQLiteConfirmationStore(
            token_factory=lambda: "persistent-generation-bool-1234"
        )
        with patch("app.agent.confirmation.secrets.randbits", return_value=1):
            self.assertEqual(store.owner_generation(owner="owner-a"), 1)
        with self.assertRaises(AgentToolError) as invalid:
            store.issue(
                owner="owner-a",
                tool_name="write.test",
                arguments={},
                expected_owner_generation=True,
            )
        self.assertEqual(invalid.exception.code, "confirmation_invalid")
        self.assertEqual(store.list_active_tickets(owner="owner-a"), [])

    def test_claim_rejects_non_string_plan_id_without_consuming_ticket(self):
        store = SQLiteConfirmationStore(
            token_factory=lambda: "persistent-strict-plan-id-1234"
        )
        ticket = store.issue(
            owner="owner-a", tool_name="write.test", arguments={"id": 1}
        )

        class StringLikePlanId:
            def __str__(self) -> str:
                return ticket.confirmation_id

        with self.assertRaises(AgentToolError) as invalid:
            store.claim_and_rotate_owner(
                owner="owner-a", confirmation_id=StringLikePlanId()
            )
        self.assertEqual(invalid.exception.code, "confirmation_invalid")
        self.assertEqual(
            store.claim_and_rotate_owner(
                owner="owner-a", confirmation_id=ticket.confirmation_id
            ).arguments,
            {"id": 1},
        )

    def test_corrupted_owner_epoch_is_rotated_without_leaking_raw_error(self):
        store = SQLiteConfirmationStore(
            token_factory=lambda: "persistent-corrupt-epoch-1234"
        )
        ticket = store.issue(
            owner="owner-a", tool_name="write.test", arguments={"id": 1}
        )
        owner_digest = store._owner_digest("owner-a")
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE agent_confirmation_epochs SET generation=? WHERE owner_digest=?",
                ("corrupt", owner_digest),
            )
        recovered_generation = store.owner_generation(owner="owner-a")
        self.assertGreater(recovered_generation, 0)
        self.assertNotEqual(recovered_generation, ticket.owner_generation)
        self.assertEqual(store.list_active_tickets(owner="owner-a"), [])
        with self.assertRaises(AgentToolError) as invalid:
            store.claim_and_rotate_owner(
                owner="owner-a", confirmation_id=ticket.confirmation_id
            )
        self.assertEqual(invalid.exception.code, "confirmation_invalid")

    def test_ticket_survives_store_recreation_and_owner_is_hashed(self) -> None:
        first = SQLiteConfirmationStore(
            token_factory=lambda: "persistent-ticket-1234567890"
        )
        ticket = first.issue(
            owner="owner-a",
            tool_name="write.test",
            arguments={"items": ["one"]},
            context_fingerprint="snapshot",
            followup_context={"episode": 3},
            confirmation_contract={"action": "测试写入"},
        )
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT owner_digest FROM agent_confirmations WHERE confirmation_id=?",
                (ticket.confirmation_id,),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertNotEqual(row["owner_digest"], "owner-a")
        self.assertRegex(str(row["owner_digest"]), "^[0-9a-f]{64}$")
        claimed = SQLiteConfirmationStore().claim_and_rotate_owner(
            owner="owner-a", confirmation_id=ticket.confirmation_id
        )
        self.assertEqual(claimed.arguments, {"items": ["one"]})
        self.assertEqual(claimed.context_fingerprint, "snapshot")
        self.assertEqual(claimed.followup_context, {"episode": 3})
        with self.assertRaises(AgentToolError):
            first.claim_and_rotate_owner(
                owner="owner-a", confirmation_id=ticket.confirmation_id
            )

    def test_invalid_json_payload_is_rejected_before_sqlite_mutation(self) -> None:
        token_factory = Mock(return_value="persistent-json-validation-1234")
        store = SQLiteConfirmationStore(max_entries=1, token_factory=token_factory)
        existing = store.issue(
            owner="owner-a", tool_name="write.test", arguments={"id": "old"}
        )
        token_factory.reset_mock()
        with self.assertRaises(AgentToolError) as invalid:
            store.issue(
                owner="owner-b",
                tool_name="write.test",
                arguments={"value": float("nan")},
            )
        self.assertEqual(invalid.exception.code, "confirmation_invalid")
        token_factory.assert_not_called()
        self.assertEqual(
            store.claim_and_rotate_owner(
                owner="owner-a", confirmation_id=existing.confirmation_id
            ).arguments,
            {"id": "old"},
        )

    def test_corrupted_sqlite_ticket_is_revoked_without_execution_claim(self) -> None:
        tokens = iter(
            ("persistent-corrupt-selected-1234", "persistent-corrupt-sibling-12345")
        )
        store = SQLiteConfirmationStore(token_factory=lambda: next(tokens))
        selected = store.issue(
            owner="owner-a", tool_name="write.test", arguments={"id": "selected"}
        )
        sibling = store.issue(
            owner="owner-a", tool_name="write.test", arguments={"id": "sibling"}
        )
        previous_generation = selected.owner_generation
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE agent_confirmations SET arguments_json=? WHERE confirmation_id=?",
                ("{broken", selected.confirmation_id),
            )
        risk_for = Mock(return_value=RiskLevel.WRITE)
        with self.assertRaises(AgentToolError) as invalid:
            store.claim_and_rotate_owner(
                owner="owner-a",
                confirmation_id=selected.confirmation_id,
                record_execution=True,
                execution_risk_for=risk_for,
            )
        self.assertEqual(invalid.exception.code, "confirmation_invalid")
        risk_for.assert_not_called()
        self.assertEqual(store.list_active_tickets(owner="owner-a"), [])
        self.assertNotEqual(
            store.owner_generation(owner="owner-a"), previous_generation
        )
        with self.assertRaises(AgentToolError):
            store.claim_and_rotate_owner(
                owner="owner-a", confirmation_id=sibling.confirmation_id
            )

    def test_listing_corrupted_sqlite_ticket_revokes_owner_group(self) -> None:
        tokens = iter(
            ("persistent-corrupt-list-first-12", "persistent-corrupt-list-second-1")
        )
        store = SQLiteConfirmationStore(token_factory=lambda: next(tokens))
        first = store.issue(
            owner="owner-a", tool_name="write.test", arguments={"id": 1}
        )
        second = store.issue(
            owner="owner-a", tool_name="write.test", arguments={"id": 2}
        )
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE agent_confirmations SET confirmation_contract_json=? WHERE confirmation_id=?",
                ("[]", second.confirmation_id),
            )
        self.assertEqual(store.list_active_tickets(owner="owner-a"), [])
        for ticket in (first, second):
            with self.assertRaises(AgentToolError):
                store.claim_and_rotate_owner(
                    owner="owner-a", confirmation_id=ticket.confirmation_id
                )

    def test_provider_confirmation_claim_audit_keeps_safe_plan_reference(self) -> None:
        plan_ref = "PP-" + "B" * 24
        store = SQLiteConfirmationStore(
            token_factory=lambda: "provider-plan-audit-ticket-1234"
        )
        ticket = store.issue(
            owner="owner-a",
            tool_name="provider.change.execute",
            arguments={"plan_ref": plan_ref},
        )
        claimed = store.claim_and_rotate_owner(
            owner="owner-a",
            confirmation_id=ticket.confirmation_id,
            record_execution=True,
            execution_risk_for=lambda _tool_name: RiskLevel.WRITE,
        )
        self.assertEqual(claimed.arguments["plan_ref"], plan_ref)
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT safe_details FROM agent_action_history WHERE confirmation_id=?",
                (f"{ticket.confirmation_id}-{ticket.owner_generation}",),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(json.loads(str(row["safe_details"])), {"plan_ref": plan_ref})

    def test_concurrent_claim_is_atomic_across_store_instances(self) -> None:
        issuer = SQLiteConfirmationStore(
            token_factory=lambda: "concurrent-ticket-123456789"
        )
        ticket = issuer.issue(owner="owner-a", tool_name="write.test", arguments={})
        barrier = threading.Barrier(2)

        def claim_once() -> str:
            barrier.wait(timeout=3)
            try:
                SQLiteConfirmationStore().claim_and_rotate_owner(
                    owner="owner-a", confirmation_id=ticket.confirmation_id
                )
            except AgentToolError as exc:
                return exc.code
            return "claimed"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = sorted(pool.map(lambda _index: claim_once(), range(2)))
        self.assertEqual(outcomes, ["claimed", "confirmation_invalid"])

    def test_claim_and_rotate_is_atomic_for_two_tickets_across_instances(self) -> None:
        tokens = iter(
            ("atomic-ticket-first-123456789", "atomic-ticket-second-12345678")
        )
        issuer = SQLiteConfirmationStore(token_factory=lambda: next(tokens))
        tickets = [
            issuer.issue(owner="owner-a", tool_name="write.test", arguments={})
            for _ in range(2)
        ]
        barrier = threading.Barrier(2)

        def claim(ticket_id: str) -> str:
            barrier.wait(timeout=3)
            try:
                SQLiteConfirmationStore().claim_and_rotate_owner(
                    owner="owner-a", confirmation_id=ticket_id
                )
            except AgentToolError as exc:
                return exc.code
            return "claimed"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = sorted(
                pool.map(claim, [item.confirmation_id for item in tickets])
            )
        self.assertEqual(outcomes, ["claimed", "confirmation_invalid"])

    def test_non_replacement_issue_at_capacity_keeps_sqlite_bounded(self) -> None:
        tokens = iter(
            ("persistent-capacity-owner-a-old", "persistent-capacity-owner-a-new")
        )
        ticks = iter((100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0))
        store = SQLiteConfirmationStore(
            max_entries=1, clock=lambda: next(ticks), token_factory=lambda: next(tokens)
        )
        previous = store.issue(
            owner="owner-a", tool_name="write.test", arguments={"id": "old"}
        )
        current = store.issue(
            owner="owner-a", tool_name="write.test", arguments={"id": "new"}
        )
        self.assertEqual(len(store.list_active_tickets(owner="owner-a")), 1)
        with self.assertRaises(AgentToolError):
            store.claim_and_rotate_owner(
                owner="owner-a", confirmation_id=previous.confirmation_id
            )
        self.assertEqual(
            store.claim_and_rotate_owner(
                owner="owner-a", confirmation_id=current.confirmation_id
            ).arguments,
            {"id": "new"},
        )

    def test_replacing_ticket_at_capacity_preserves_other_owner(self) -> None:
        tokens = iter(
            (
                "persistent-capacity-owner-b-1",
                "persistent-capacity-owner-a-old",
                "persistent-capacity-owner-a-new",
            )
        )
        ticks = iter((100.0, 101.0, 102.0, 103.0, 104.0, 105.0))
        store = SQLiteConfirmationStore(
            max_entries=2, clock=lambda: next(ticks), token_factory=lambda: next(tokens)
        )
        other = store.issue(
            owner="owner-b", tool_name="write.test", arguments={"id": "b"}
        )
        previous = store.issue(
            owner="owner-a", tool_name="write.test", arguments={"id": "old"}
        )
        replacement = store.issue(
            owner="owner-a",
            tool_name="write.test",
            arguments={"id": "new"},
            replace_active_ticket=True,
        )
        self.assertEqual(
            store.claim_and_rotate_owner(
                owner="owner-b", confirmation_id=other.confirmation_id
            ).arguments,
            {"id": "b"},
        )
        with self.assertRaises(AgentToolError):
            store.claim_and_rotate_owner(
                owner="owner-a", confirmation_id=previous.confirmation_id
            )
        self.assertEqual(
            store.claim_and_rotate_owner(
                owner="owner-a", confirmation_id=replacement.confirmation_id
            ).arguments,
            {"id": "new"},
        )

    def test_owner_rotation_can_preserve_ticket_across_store_instances(self) -> None:
        first = SQLiteConfirmationStore(
            token_factory=lambda: "preserved-ticket-123456789"
        )
        ticket = first.issue(
            owner="owner-a", tool_name="write.test", arguments={"id": 1}
        )
        revoked, generation = SQLiteConfirmationStore().rotate_owner(
            owner="owner-a", preserve_active=True
        )
        self.assertEqual(revoked, 0)
        active = SQLiteConfirmationStore().list_active_tickets(owner="owner-a")
        self.assertEqual(
            [item.confirmation_id for item in active], [ticket.confirmation_id]
        )
        self.assertEqual(active[0].owner_generation, generation)
        self.assertEqual(
            SQLiteConfirmationStore()
            .claim_and_rotate_owner(
                owner="owner-a", confirmation_id=ticket.confirmation_id
            )
            .arguments,
            {"id": 1},
        )

    def test_owner_rotation_revokes_tickets_across_instances(self) -> None:
        first = SQLiteConfirmationStore(
            token_factory=lambda: "rotated-ticket-1234567890"
        )
        ticket = first.issue(owner="owner-a", tool_name="write.test", arguments={})
        revoked, generation = SQLiteConfirmationStore().rotate_owner(owner="owner-a")
        self.assertEqual(revoked, 1)
        self.assertGreater(generation, 0)
        with self.assertRaises(AgentToolError):
            first.claim_and_rotate_owner(
                owner="owner-a", confirmation_id=ticket.confirmation_id
            )
