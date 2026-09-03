"""Agent 受控写操作确认协议与 STRM 首个动作测试。"""

from __future__ import annotations

import threading
import unittest
from unittest.mock import Mock, patch

from app.agent.confirmation import ConfirmationStore, confirmation_reply_intent
from app.agent.confirmation_contract import (
    build_confirmation_contract,
    sanitize_confirmation_contract,
)
from app.agent.errors import AgentToolError
from app.agent.models import RiskLevel, ToolResult


class ConfirmationStoreTests(unittest.TestCase):
    def test_expected_owner_generation_rejects_bool_even_when_epoch_is_one(self):
        store = ConfirmationStore(token_factory=lambda: "ticket-generation-bool-1234")
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
        store = ConfirmationStore(token_factory=lambda: "ticket-strict-plan-id-123456")
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

    def test_media_subscription_confirmations_have_specific_public_copy(self):
        preview = ToolResult(True, "preview", "预检通过")
        created = build_confirmation_contract(
            tool_name="media.create_subscription",
            risk=RiskLevel.LOW_WRITE,
            preview=preview,
        )
        deleted = build_confirmation_contract(
            tool_name="media.delete_subscription",
            risk=RiskLevel.DANGER,
            preview=preview,
        )
        self.assertEqual(created["action"], "创建媒体追更订阅")
        self.assertIn("不会立即搜索资源", created["impact"])
        self.assertEqual(deleted["action"], "删除媒体追更订阅")
        self.assertIn("删除不可撤销", deleted["reversibility"])

    def test_confirmation_contract_rejects_boolean_version(self):
        contract = build_confirmation_contract(
            tool_name="media.create_subscription",
            risk=RiskLevel.LOW_WRITE,
            preview=ToolResult(True, "preview", "预检通过"),
        )
        contract["version"] = True
        self.assertEqual(sanitize_confirmation_contract(contract), {})

    def test_confirmation_contract_rejects_coercible_version(self):
        for version in ("1", 1.0):
            with self.subTest(version=version):
                contract = build_confirmation_contract(
                    tool_name="media.create_subscription",
                    risk=RiskLevel.LOW_WRITE,
                    preview=ToolResult(True, "preview", "预检通过"),
                )
                contract["version"] = version
                self.assertEqual(sanitize_confirmation_contract(contract), {})

    def test_confirmation_contract_rejects_fabricated_time_or_risk(self):
        contract = build_confirmation_contract(
            tool_name="media.create_subscription",
            risk=RiskLevel.DANGER,
            preview=ToolResult(True, "preview", "预检通过"),
            preflight_at="2026-08-31T12:00:00+08:00",
        )
        for value in (None, "", "not-a-time", "2026-08-31T12:00:00", True):
            with self.subTest(preflight_at=value):
                tampered = dict(contract)
                tampered["preflight_at"] = value
                self.assertEqual(sanitize_confirmation_contract(tampered), {})
        for risk in (None, "", "read", "root"):
            with self.subTest(risk=risk):
                tampered = dict(contract)
                tampered["risk"] = risk
                self.assertEqual(sanitize_confirmation_contract(tampered), {})
        with self.assertRaises(ValueError):
            build_confirmation_contract(
                tool_name="media.create_subscription",
                risk=RiskLevel.DANGER,
                preview=ToolResult(True, "preview", "预检通过"),
                preflight_at="not-a-time",
            )

    def test_confirmation_reply_intent_is_explicit_and_non_ambiguous(self):
        for value in ("确认", "好的帮我执行。", "YES", "取消", "算了！"):
            with self.subTest(value=value):
                self.assertIsNotNone(confirmation_reply_intent(value))
        for value in ("请确认状态", "确认第三个", "好的，但是先检查", "不要取消"):
            with self.subTest(value=value):
                self.assertIsNone(confirmation_reply_intent(value))

    def test_listing_unknown_owner_is_read_only(self):
        store = ConfirmationStore()
        with patch("app.agent.confirmation.secrets.randbits") as random_generation:
            self.assertEqual(store.list_active_tickets(owner="owner-missing"), [])
        random_generation.assert_not_called()

    def test_claim_generation_failure_does_not_record_execution(self):
        store = ConfirmationStore(token_factory=lambda: "ticket-audit-order-123456")
        ticket = store.issue(
            owner="owner-a", tool_name="write.test", arguments={"id": 1}
        )
        with (
            patch(
                "app.agent.confirmation.secrets.randbits",
                return_value=ticket.owner_generation,
            ),
            patch(
                "app.agent.action_history.record_confirmation_claimed"
            ) as record_claimed,self.assertRaises(AgentToolError) as unavailable
        ):
            store.claim_and_rotate_owner(
                owner="owner-a",
                confirmation_id=ticket.confirmation_id,
                record_execution=True,
            )
        self.assertEqual(unavailable.exception.code, "confirmation_unavailable")
        record_claimed.assert_not_called()
        self.assertEqual(
            store.claim_and_rotate_owner(
                owner="owner-a", confirmation_id=ticket.confirmation_id
            ).arguments,
            {"id": 1},
        )

    def test_list_active_tickets_returns_current_generation_without_consuming(self):
        store = ConfirmationStore(token_factory=lambda: "ticket-list-active-123456")
        ticket = store.issue(
            owner="owner-a", tool_name="write.test", arguments={"id": 1}
        )
        first = store.list_active_tickets(owner="owner-a")
        second = store.list_active_tickets(owner="owner-a")
        self.assertEqual(
            [item.confirmation_id for item in first], [ticket.confirmation_id]
        )
        self.assertEqual(
            [item.confirmation_id for item in second], [ticket.confirmation_id]
        )
        self.assertEqual(
            store.claim_and_rotate_owner(
                owner="owner-a", confirmation_id=ticket.confirmation_id
            ).arguments,
            {"id": 1},
        )

    def test_claim_and_rotate_revokes_same_owner_but_not_other_owner(self):
        tokens = iter(
            (
                "ticket-owner-a-first-123456",
                "ticket-owner-a-second-12345",
                "ticket-owner-b-first-123456",
            )
        )
        store = ConfirmationStore(token_factory=lambda: next(tokens))
        first = store.issue(owner="owner-a", tool_name="write.test", arguments={})
        second = store.issue(owner="owner-a", tool_name="write.test", arguments={})
        other = store.issue(owner="owner-b", tool_name="write.test", arguments={})
        claimed = store.claim_and_rotate_owner(
            owner="owner-a", confirmation_id=first.confirmation_id
        )
        self.assertEqual(claimed.confirmation_id, first.confirmation_id)
        with self.assertRaises(AgentToolError):
            store.claim_and_rotate_owner(
                owner="owner-a", confirmation_id=second.confirmation_id
            )
        self.assertEqual(
            store.claim_and_rotate_owner(
                owner="owner-b", confirmation_id=other.confirmation_id
            ).owner,
            "owner-b",
        )

    def test_ticket_is_single_use_and_arguments_are_copied(self):
        store = ConfirmationStore(token_factory=lambda: "ticket-1234567890abcdef")
        arguments = {"items": ["one"]}
        followup_context = {"verification": {"episode": 3}}
        confirmation_contract = {
            "version": 1,
            "action": "提交下载任务",
            "object": "候选资源",
            "impact": "会创建下载任务",
            "reversibility": "可在下载器中删除",
            "preflight_at": "2026-08-09T12:34:56+08:00",
            "risk": "danger",
            "preflight_summary": "预检通过",
        }
        ticket = store.issue(
            owner="owner-a",
            tool_name="write.test",
            arguments=arguments,
            followup_context=followup_context,
            confirmation_contract=confirmation_contract,
        )
        arguments["items"].append("changed")
        followup_context["verification"]["episode"] = 4
        confirmation_contract["object"] = "已被调用方修改"
        claimed = store.claim_and_rotate_owner(
            owner="owner-a", confirmation_id=ticket.confirmation_id
        )
        self.assertEqual(claimed.arguments, {"items": ["one"]})
        self.assertEqual(claimed.followup_context, {"verification": {"episode": 3}})
        self.assertEqual(claimed.confirmation_contract["object"], "候选资源")
        claimed.followup_context["verification"]["episode"] = 5
        claimed.confirmation_contract["impact"] = "已被领取方修改"
        self.assertEqual(ticket.followup_context, {"verification": {"episode": 3}})
        self.assertEqual(ticket.confirmation_contract["impact"], "会创建下载任务")
        with self.assertRaises(AgentToolError) as replay:
            store.claim_and_rotate_owner(
                owner="owner-a", confirmation_id=ticket.confirmation_id
            )
        self.assertEqual(replay.exception.code, "confirmation_invalid")

    def test_returned_ticket_cannot_mutate_pending_execution_payload(self):
        store = ConfirmationStore(token_factory=lambda: "ticket-detached-return-123456")
        ticket = store.issue(
            owner="owner-a",
            tool_name="write.test",
            arguments={"items": ["one"]},
            followup_context={"verification": {"episode": 3}},
            confirmation_contract={"action": "测试动作"},
        )
        ticket.arguments["items"].append("tampered")
        ticket.followup_context["verification"]["episode"] = 99
        ticket.confirmation_contract["action"] = "已篡改"
        claimed = store.claim_and_rotate_owner(
            owner="owner-a", confirmation_id=ticket.confirmation_id
        )
        self.assertEqual(claimed.arguments, {"items": ["one"]})
        self.assertEqual(claimed.followup_context, {"verification": {"episode": 3}})
        self.assertEqual(claimed.confirmation_contract, {"action": "测试动作"})

    def test_invalid_json_payload_is_rejected_before_store_mutation(self):
        token_factory = Mock(return_value="ticket-json-validation-123456")
        store = ConfirmationStore(max_entries=1, token_factory=token_factory)
        existing = store.issue(
            owner="owner-a", tool_name="write.test", arguments={"id": "old"}
        )
        token_factory.reset_mock()
        invalid_calls = (
            {"arguments": []},
            {"arguments": {"value": float("nan")}},
            {"arguments": {"value": object()}},
            {"arguments": {"value": "\ud800"}},
            {"arguments": {}, "followup_context": []},
            {"arguments": {}, "confirmation_contract": []},
        )
        for kwargs in invalid_calls:
            with self.subTest(kwargs=tuple(kwargs)):
                with self.assertRaises(AgentToolError) as invalid:
                    store.issue(owner="owner-b", tool_name="write.test", **kwargs)
                self.assertEqual(invalid.exception.code, "confirmation_invalid")
                self.assertEqual(token_factory.call_count, 0)
                self.assertEqual(
                    [
                        item.confirmation_id
                        for item in store.list_active_tickets(owner="owner-a")
                    ],
                    [existing.confirmation_id],
                )
        self.assertEqual(
            store.claim_and_rotate_owner(
                owner="owner-a", confirmation_id=existing.confirmation_id
            ).arguments,
            {"id": "old"},
        )

    def test_expiry_and_wrong_owner_do_not_leak_or_steal_ticket(self):
        now = [100.0]
        store = ConfirmationStore(
            ttl_seconds=10,
            clock=lambda: now[0],
            token_factory=lambda: "ticket-abcdefghijklmnop",
        )
        ticket = store.issue(owner="owner-a", tool_name="write.test", arguments={})
        with self.assertRaises(AgentToolError):
            store.claim_and_rotate_owner(
                owner="owner-b", confirmation_id=ticket.confirmation_id
            )
        claimed = store.claim_and_rotate_owner(
            owner="owner-a", confirmation_id=ticket.confirmation_id
        )
        self.assertEqual(claimed.owner, "owner-a")
        ticket = store.issue(owner="owner-a", tool_name="write.test", arguments={})
        now[0] += 10
        with self.assertRaises(AgentToolError) as expired:
            store.claim_and_rotate_owner(
                owner="owner-a", confirmation_id=ticket.confirmation_id
            )
        self.assertEqual(expired.exception.code, "confirmation_invalid")

    def test_owner_generation_rejects_ticket_issued_after_reset_race(self):
        store = ConfirmationStore(token_factory=lambda: "ticket-generation-123456")
        generation = store.owner_generation(owner="owner-a")
        self.assertEqual(store.revoke_owner(owner="owner-a"), 0)
        with self.assertRaises(AgentToolError) as stale:
            store.issue(
                owner="owner-a",
                tool_name="write.test",
                arguments={},
                expected_owner_generation=generation,
            )
        self.assertEqual(stale.exception.code, "confirmation_invalid")

    def test_failed_token_generation_preserves_existing_ticket(self):
        tokens = iter(("ticket-preserved-on-token-failure",) + ("",) * 8)
        store = ConfirmationStore(max_entries=1, token_factory=lambda: next(tokens))
        existing = store.issue(
            owner="owner-a", tool_name="write.test", arguments={"id": "old"}
        )
        with self.assertRaises(AgentToolError) as raised:
            store.issue(
                owner="owner-b", tool_name="write.test", arguments={"id": "new"}
            )
        self.assertEqual(raised.exception.code, "confirmation_unavailable")
        self.assertEqual(
            store.claim_and_rotate_owner(
                owner="owner-a", confirmation_id=existing.confirmation_id
            ).arguments,
            {"id": "old"},
        )

    def test_non_replacement_issue_at_capacity_evicts_oldest_same_owner_ticket(self):
        tokens = iter(
            ("ticket-capacity-owner-a-old-1", "ticket-capacity-owner-a-new-1")
        )
        ticks = iter((100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0))
        store = ConfirmationStore(
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

    def test_replacing_plan_at_capacity_does_not_evict_other_owner(self):
        tokens = iter(
            (
                "ticket-capacity-owner-b-1234",
                "ticket-capacity-owner-a-old-1",
                "ticket-capacity-owner-a-new-1",
            )
        )
        now = iter((100.0, 101.0, 102.0, 103.0, 104.0, 105.0))
        store = ConfirmationStore(
            max_entries=2, clock=lambda: next(now), token_factory=lambda: next(tokens)
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

    def test_rotate_owner_can_preserve_active_ticket_under_new_epoch(self):
        store = ConfirmationStore(token_factory=lambda: "ticket-preserved-query-123456")
        ticket = store.issue(
            owner="owner-a", tool_name="write.test", arguments={"id": 1}
        )
        original_epoch = ticket.owner_generation
        revoked, current_epoch = store.rotate_owner(
            owner="owner-a", preserve_active=True
        )
        self.assertEqual(revoked, 0)
        self.assertNotEqual(current_epoch, original_epoch)
        active = store.list_active_tickets(owner="owner-a")
        self.assertEqual(
            [item.confirmation_id for item in active], [ticket.confirmation_id]
        )
        self.assertEqual(active[0].owner_generation, current_epoch)
        with self.assertRaises(AgentToolError) as stale:
            store.issue(
                owner="owner-a",
                tool_name="write.test",
                arguments={},
                expected_owner_generation=original_epoch,
            )
        self.assertEqual(stale.exception.code, "confirmation_invalid")

    def test_rotate_owner_revokes_existing_tickets_and_returns_new_epoch(self):
        store = ConfirmationStore(token_factory=lambda: "ticket-rotate-1234567890")
        previous_epoch = store.owner_generation(owner="owner-a")
        ticket = store.issue(owner="owner-a", tool_name="write.test", arguments={})
        revoked, current_epoch = store.rotate_owner(owner="owner-a")
        self.assertEqual(revoked, 1)
        self.assertNotEqual(current_epoch, previous_epoch)
        self.assertEqual(store.owner_generation(owner="owner-a"), current_epoch)
        with self.assertRaises(AgentToolError) as stale:
            store.claim_and_rotate_owner(
                owner="owner-a", confirmation_id=ticket.confirmation_id
            )
        self.assertEqual(stale.exception.code, "confirmation_invalid")

    def test_pruned_owner_epoch_never_accepts_pre_reset_prepare(self):
        now = [100.0]
        store = ConfirmationStore(
            ttl_seconds=10,
            clock=lambda: now[0],
            token_factory=lambda: "ticket-pruned-generation-1234",
        )
        generation = store.owner_generation(owner="owner-a")
        store.revoke_owner(owner="owner-a")
        now[0] += 21
        with self.assertRaises(AgentToolError) as stale:
            store.issue(
                owner="owner-a",
                tool_name="write.test",
                arguments={},
                expected_owner_generation=generation,
            )
        self.assertEqual(stale.exception.code, "confirmation_invalid")

    def test_atomic_claim_allows_only_one_consumer(self):
        store = ConfirmationStore(token_factory=lambda: "ticket-concurrent-123456")
        ticket = store.issue(owner="owner", tool_name="write.test", arguments={})
        barrier = threading.Barrier(3)
        outcomes: list[str] = []

        def claim() -> None:
            barrier.wait()
            try:
                store.claim_and_rotate_owner(
                    owner="owner", confirmation_id=ticket.confirmation_id
                )
                outcomes.append("ok")
            except AgentToolError:
                outcomes.append("blocked")

        threads = [threading.Thread(target=claim) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertCountEqual(outcomes, ["ok", "blocked"])

    def test_discard_and_owner_revoke_are_isolated(self):
        tokens = iter(
            (
                "ticket-owner-a-00000001",
                "ticket-owner-a-00000002",
                "ticket-owner-b-00000001",
            )
        )
        store = ConfirmationStore(token_factory=lambda: next(tokens))
        first = store.issue(owner="owner-a", tool_name="write.test", arguments={})
        second = store.issue(owner="owner-a", tool_name="write.test", arguments={})
        other = store.issue(owner="owner-b", tool_name="write.test", arguments={})
        self.assertFalse(
            store.discard(owner="owner-b", confirmation_id=first.confirmation_id)
        )
        self.assertTrue(
            store.discard(owner="owner-a", confirmation_id=first.confirmation_id)
        )
        self.assertEqual(store.revoke_owner(owner="owner-a"), 1)
        with self.assertRaises(AgentToolError):
            store.claim_and_rotate_owner(
                owner="owner-a", confirmation_id=second.confirmation_id
            )
        self.assertEqual(
            store.claim_and_rotate_owner(
                owner="owner-b", confirmation_id=other.confirmation_id
            ).owner,
            "owner-b",
        )
