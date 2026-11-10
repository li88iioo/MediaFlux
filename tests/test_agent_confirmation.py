"""Agent 受控写操作确认协议与光鸭单入口完整计划合同测试。"""

from __future__ import annotations
from typing import Any, cast

import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime
from unittest.mock import Mock, patch

from app.agent.confirmation import (
    ConfirmationExpiredError,
    ConfirmationStore,
    SQLiteConfirmationStore,
)
from app.agent.confirmation_contract import (
    build_confirmation_contract,
    sanitize_confirmation_contract,
)
from app.agent.errors import AgentToolError, ToolPipelineError
from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.effects import ConfirmationEffectPlanStore
from app.agent.kernel.tool_calls import PreparedEffect
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.references import InMemoryReferenceStore, ReferenceError
from app.agent.kernel.state import InMemorySessionStateStore
from app.agent.kernel.state_types import CancellationToken, StateUpdate
from app.agent.kernel.tool_calls import ToolCallContext
from app.agent.models import RiskLevel, ToolResult
from tests.support import isolated_test_database
from app import sdk
import pytest


class ConfirmationStoreTests(unittest.TestCase):
    def test_expired_evidence_is_once_only_private_and_cannot_revoke_other_live_ticket(self):
        for store_type in (ConfirmationStore, SQLiteConfirmationStore):
            with self.subTest(store=store_type.__name__), isolated_test_database():
                now = [1000.0]
                store = store_type(ttl_seconds=10, clock=lambda: now[0])
                old = store.issue(owner="owner", tool_name="write.test", arguments={"private": "original"})
                now[0] += 5
                new = store.issue(owner="owner", tool_name="write.test", arguments={"private": "new"})
                now[0] += 6
                self.assertEqual(store.list_active_tickets(owner="owner"), [new])
                self.assertIsNone(store.ticket_owner_match(owner="owner", confirmation_id=old.confirmation_id))
                self.assertIsNone(store.inspect_ticket(owner="foreign", confirmation_id=old.confirmation_id))
                self.assertEqual(store.inspect_ticket(owner="owner", confirmation_id=old.confirmation_id), old)
                with patch("app.agent.action_history.record_confirmation_claimed") as audit:
                    with self.assertRaises(ConfirmationExpiredError) as expired:
                        store.claim_and_rotate_owner(owner="owner", confirmation_id=old.confirmation_id, record_execution=True)
                audit.assert_not_called()
                self.assertEqual(expired.exception.ticket, old)
                self.assertIsNone(store.inspect_ticket(owner="owner", confirmation_id=old.confirmation_id))
                with self.assertRaises(AgentToolError) as repeated:
                    store.claim_and_rotate_owner(owner="owner", confirmation_id=old.confirmation_id)
                self.assertNotIsInstance(repeated.exception, ConfirmationExpiredError)
                self.assertEqual(store.claim_and_rotate_owner(owner="owner", confirmation_id=new.confirmation_id), new)

    def test_expired_evidence_retention_does_not_slide_or_survive_owner_rotation(self):
        for store_type in (ConfirmationStore, SQLiteConfirmationStore):
            for boundary in ("retention", "revoke", "preserve_active", "replacement", "reset", "capacity"):
                with self.subTest(store=store_type.__name__, boundary=boundary), isolated_test_database():
                    now = [1000.0]
                    store = store_type(ttl_seconds=10, max_entries=1, clock=lambda: now[0])
                    old = store.issue(owner="owner", tool_name="write.test", arguments={})
                    now[0] += 11
                    self.assertEqual(store.inspect_ticket(owner="owner", confirmation_id=old.confirmation_id), old)
                    if boundary == "retention":
                        now[0] += 9
                    elif boundary == "replacement":
                        store.issue(owner="owner", tool_name="write.test", arguments={}, replace_active_ticket=True)
                    elif boundary == "capacity":
                        store.issue(owner="another", tool_name="write.test", arguments={})
                    elif boundary == "reset":
                        store.reset()
                    else:
                        store.rotate_owner(owner="owner", preserve_active=boundary == "preserve_active")
                    self.assertIsNone(store.inspect_ticket(owner="owner", confirmation_id=old.confirmation_id))
                    with self.assertRaises(AgentToolError) as invalid:
                        store.claim_and_rotate_owner(owner="owner", confirmation_id=old.confirmation_id)
                    self.assertNotIsInstance(invalid.exception, ConfirmationExpiredError)

    def test_token_generation_delay_does_not_renew_ref_or_replace_existing_card(self):
        for store_type in (ConfirmationStore, SQLiteConfirmationStore):
            with self.subTest(store=store_type.__name__), isolated_test_database():
                monotonic_now = [100.0]
                store = store_type(clock=lambda now=monotonic_now: 2_000_000_000.0 + now[0])
                original = store.issue(owner="owner", tool_name="write.test", arguments={})

                def delayed_token(now=monotonic_now):
                    now[0] += 60
                    return "ticket-reference-expired-while-generating-token"

                store._token_factory = delayed_token
                with patch("app.agent.confirmation.time.monotonic", side_effect=lambda now=monotonic_now: now[0]), \
                        self.assertRaises(AgentToolError):
                    store.issue(owner="owner", tool_name="write.test", arguments={},
                                deadline=150.0, replace_active_ticket=True)
                self.assertEqual([t.confirmation_id for t in store.list_active_tickets(owner="owner")],
                                 [original.confirmation_id])

    def test_reference_deadline_caps_ticket_without_replacing_on_expired_budget(self):
        for store_type in (ConfirmationStore, SQLiteConfirmationStore):
            with self.subTest(store=store_type.__name__), isolated_test_database():
                clock = [2_000_000_000.0]
                store = store_type(clock=lambda now=clock: now[0])
                deadline = time.monotonic() + 0.25
                capped = store.issue(owner="owner", tool_name="write.test", arguments={}, deadline=deadline)
                self.assertGreater(capped.expires_at, clock[0])
                self.assertLessEqual(capped.expires_at, clock[0] + 0.25)
                with self.assertRaises(AgentToolError):
                    store.issue(owner="owner", tool_name="write.test", arguments={},
                                deadline=time.monotonic() - 1, replace_active_ticket=True)
                self.assertEqual([t.confirmation_id for t in store.list_active_tickets(owner="owner")],
                                 [capped.confirmation_id])
                ordinary = store.issue(owner="other", tool_name="write.test", arguments={},
                                       deadline=time.monotonic() + 900)
                self.assertEqual(ordinary.expires_at, clock[0] + 600)

    def test_default_confirmation_window_allows_review_but_still_expires(self):
        for store_type in (ConfirmationStore, SQLiteConfirmationStore):
            with self.subTest(store=store_type.__name__), isolated_test_database():
                now = [1000.0]
                store = store_type(clock=lambda: now[0])
                ticket = store.issue(owner="owner", tool_name="cloud.change", arguments={})
                self.assertEqual(ticket.expires_at - now[0], 600)
                # 复现实例：用户看完批量方案，约五分钟后点击确认。
                now[0] += 306
                claimed = store.claim_and_rotate_owner(owner="owner", confirmation_id=ticket.confirmation_id)
                self.assertEqual(claimed.confirmation_id, ticket.confirmation_id)
                with self.assertRaises(AgentToolError):
                    store.claim_and_rotate_owner(owner="owner", confirmation_id=ticket.confirmation_id)

                ticket = store.issue(owner="owner", tool_name="cloud.change", arguments={})
                now[0] = ticket.expires_at
                with self.assertRaises(AgentToolError) as expired:
                    store.claim_and_rotate_owner(owner="owner", confirmation_id=ticket.confirmation_id)
                self.assertEqual(expired.exception.code, "confirmation_invalid")

    def test_new_default_does_not_extend_persisted_or_explicit_confirmation_expiry(self):
        with isolated_test_database():
            now = [1000.0]
            old_store = SQLiteConfirmationStore(ttl_seconds=60, clock=lambda: now[0])
            old_ticket = old_store.issue(owner="owner", tool_name="write.test", arguments={})
            rebuilt = SQLiteConfirmationStore(clock=lambda: now[0])
            self.assertEqual(rebuilt.list_active_tickets(owner="owner")[0].expires_at, 1060.0)
            now[0] += 61
            with self.assertRaises(AgentToolError):
                rebuilt.claim_and_rotate_owner(owner="owner", confirmation_id=old_ticket.confirmation_id)
            self.assertEqual(rebuilt.list_active_tickets(owner="owner"), [])

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
                owner="owner-a", confirmation_id=cast(str, StringLikePlanId())
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
            tool_name="subscriptions.create",
            risk=RiskLevel.LOW_WRITE,
            preview=preview,
        )
        deleted = build_confirmation_contract(
            tool_name="subscriptions.delete",
            risk=RiskLevel.DANGER,
            preview=preview,
        )
        self.assertEqual(created["action"], "创建媒体追更订阅")
        self.assertIn("不会立即搜索资源", created["impact"])
        self.assertEqual(deleted["action"], "删除媒体追更订阅")
        self.assertIn("可通过同媒体创建恢复", deleted["reversibility"])

    def test_guangya_fs_confirmation_copy_separates_preparation_from_cloud_execution(self):
        contract = build_confirmation_contract(
            tool_name="cloud.change",
            risk=RiskLevel.DANGER,
            preview=ToolResult(True, "confirmation_required", "完整计划已冻结，等待用户确认"),
        )
        self.assertEqual(contract["action"], "执行光鸭文件变更")
        self.assertIn("完整", contract["object"])
        self.assertIn("复制", contract["object"])
        self.assertIn("准备确认卡不写云端", contract["impact"])
        self.assertIn("确认后只执行卡中固定的完整计划", contract["impact"])
        self.assertIn("写前复核", contract["impact"])
        self.assertIn("写后逐项验证", contract["impact"])
        self.assertEqual(sanitize_confirmation_contract(contract), contract)

    def test_confirmation_contract_rejects_boolean_version(self):
        contract = build_confirmation_contract(
            tool_name="subscriptions.create",
            risk=RiskLevel.LOW_WRITE,
            preview=ToolResult(True, "preview", "预检通过"),
        )
        contract["version"] = True
        self.assertEqual(sanitize_confirmation_contract(contract), {})

    def test_confirmation_contract_rejects_coercible_version(self):
        for version in ("1", 1.0):
            with self.subTest(version=version):
                contract = build_confirmation_contract(
                    tool_name="subscriptions.create",
                    risk=RiskLevel.LOW_WRITE,
                    preview=ToolResult(True, "preview", "预检通过"),
                )
                contract["version"] = version
                self.assertEqual(sanitize_confirmation_contract(contract), {})

    def test_confirmation_contract_rejects_fabricated_time_or_risk(self):
        contract = build_confirmation_contract(
            tool_name="subscriptions.create",
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
                tool_name="subscriptions.create",
                risk=RiskLevel.DANGER,
                preview=ToolResult(True, "preview", "预检通过"),
                preflight_at="not-a-time",
            )

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
                    store.issue(
                        owner="owner-b", tool_name="write.test",
                        arguments=cast(dict[str, Any], kwargs["arguments"]),
                        followup_context=cast(dict[str, Any] | None, kwargs.get("followup_context")),
                        confirmation_contract=cast(dict[str, Any] | None, kwargs.get("confirmation_contract")),
                    )
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


class GuangYaFSConfirmationTests(unittest.IsolatedAsyncioTestCase):
    """真实 catalog/validator/ports/Kernel 确认链路；只替换领域 I/O 边界。"""

    async def asyncSetUp(self):
        from dataclasses import replace

        from app.agent.kernel.effects import ConfirmationEffectPlanStore
        from app.agent.kernel.pipeline import ToolPipeline
        from app.agent.kernel.state import InMemorySessionStateStore
        from app.agent.kernel.state_types import CancellationToken
        from app.agent.kernel.tool_calls import ToolCallContext

        self.enterContext(isolated_test_database())
        from app.agent.kernel.ports.sdk_effects import cloud_change_tool
        from app.agent.kernel.capabilities import ToolCatalog
        from app.agent.kernel.tool_calls import PreparedEffect
        self.spec = cloud_change_tool()
        self.validator = Mock(wraps=self.spec.validator)
        self.preparer = Mock(return_value=PreparedEffect(
            preview=ToolResult(True, "confirmation_required", "完整计划已冻结，等待用户确认", data={"total": 2}).to_dict(),
            snapshot_fingerprint="frozen-fs-fingerprint",
        ))
        self.confirmed = Mock(return_value=ToolResult(True, "success", "确认后的文件变更已验证", data={"total": 2}))
        self.catalog = ToolCatalog([replace(
            self.spec, validator=self.validator, prepare=self.preparer,
            execute_confirmed=self.confirmed, verify=None,
        )])
        self.state = InMemorySessionStateStore()
        lease, _ = await self.state.begin_turn(owner="fs-owner", session_id="fs-session", request_id="fs-request")

        async def progress(_payload):
            return None

        self.context = ToolCallContext(
            owner="fs-owner", session_id="fs-session", request_id="fs-request",
            turn_id=lease.turn_id, lease=lease, cancellation=CancellationToken(), report_progress=progress,
        )
        self.effects = ConfirmationEffectPlanStore(SQLiteConfirmationStore())
        self.pipeline = ToolPipeline(catalog=self.catalog, state_store=self.state, effect_store=self.effects)
        self.arguments = {
            "operations": [
                {"op": "rename", "file_id": "Real-case-sensitive-file-ID", "new_name": "规范名称.mkv"},
                {"op": "create_directory", "parent_path": "/", "name": "新目录"},
            ],
            "trigger_strm": False,
        }

    async def test_full_operations_prepare_one_card_and_only_confirmation_executes(self):
        prepared = await self.pipeline.execute(self.spec.name, self.arguments, context=self.context)
        self.preparer.assert_called_once()
        self.confirmed.assert_not_called()
        self.validator.assert_called_once()
        plan = prepared.effect_plan
        assert (plan) is not None
        self.assertEqual(plan.tool_name, "cloud.change")
        self.assertEqual(plan.arguments, self.spec.validator(self.arguments))
        self.assertEqual(len(plan.arguments["operations"]), len(self.arguments["operations"]))
        self.assertEqual(plan.arguments["operations"][0], self.arguments["operations"][0])
        self.assertEqual(plan.arguments["operations"][1], {"op": "create_directory", "parent_path": "/", "name": "新目录"})
        self.assertFalse(plan.arguments["trigger_strm"])
        self.assertEqual(self.preparer.call_args.args[0], plan.arguments)
        pending = await self.pipeline.get_pending_plan(owner="fs-owner", session_id="fs-session")
        assert pending is not None
        self.assertEqual(pending.plan_id, plan.plan_id)

        result = await self.pipeline.execute_confirmed(plan.plan_id, context=self.context)
        self.confirmed.assert_called_once()
        self.assertEqual(self.confirmed.call_args.args[:2], (plan.arguments, "frozen-fs-fingerprint"))
        self.validator.assert_called_once()
        self.assertTrue(result.outcome.public_content["ok"])
        self.assertIsNone(await self.pipeline.get_pending_plan(owner="fs-owner", session_id="fs-session"))

    async def test_empty_or_missing_operations_cannot_issue_or_reissue_a_card(self):
        from app.agent.errors import ToolPipelineError

        for arguments in ({}, {"operations": []}, {"trigger_strm": False}):
            with self.subTest(arguments=arguments), self.assertRaises(ToolPipelineError) as invalid:
                await self.pipeline.execute(self.spec.name, arguments, context=self.context)
            self.assertEqual(invalid.exception.code, "invalid_arguments")
        self.preparer.assert_not_called()
        self.confirmed.assert_not_called()
        self.assertIsNone(await self.pipeline.get_pending_plan(owner="fs-owner", session_id="fs-session"))

        prepared = await self.pipeline.execute(self.spec.name, self.arguments, context=self.context)
        with self.assertRaises(ToolPipelineError) as invalid:
            await self.pipeline.execute(self.spec.name, {}, context=self.context)
        self.assertEqual(invalid.exception.code, "invalid_arguments")
        self.preparer.assert_called_once()
        self.confirmed.assert_not_called()
        pending = await self.pipeline.get_pending_plan(owner="fs-owner", session_id="fs-session")
        assert pending is not None
        assert prepared.effect_plan is not None
        self.assertEqual(pending.plan_id, prepared.effect_plan.plan_id)



class ReferenceBoundConfirmationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(isolated_test_database("reference-bound-confirmation.db"))
        sdk_patch = pytest.MonkeyPatch()
        self.addCleanup(sdk_patch.undo)
        def declared(payload_ref: str):
            raise AssertionError("only existing effect handlers may write")
        for name in ("test.ref_write", "test.ref_tail"):
            sdk_patch.setitem(sdk.METHODS, name, declared)
        sdk_patch.setattr(sdk, "WRITE_METHODS", sdk.WRITE_METHODS | {"test.ref_write", "test.ref_tail"})

    async def runtime(self, *, sqlite_refs=False, sqlite_tickets=True):
        self.ref_time, self.ticket_time = 0.0, 2_000_000_000.0
        self.writes, self.prepares = [], []
        self.denied, self.handler_failure, self.verify_failure = False, False, False
        self.on_prepare, self.frozen_arguments = None, None
        self.refs = (SQLiteKernelStore(clock=lambda: self.ref_time) if sqlite_refs else
                     InMemoryReferenceStore(clock=lambda: self.ref_time))
        store_type = SQLiteConfirmationStore if sqlite_tickets else ConfirmationStore
        self.tickets = store_type(clock=lambda: self.ticket_time)
        self.state = InMemorySessionStateStore()

        def prepare(arguments, _context):
            self.prepares.append(arguments)
            if self.on_prepare:
                self.on_prepare()
            return PreparedEffect(preview={"ok": True, "summary": "引用写入预览"},
                                  arguments=self.frozen_arguments, snapshot_fingerprint="frozen")

        def execute(arguments, expected, _context):
            self.assertEqual(expected, "frozen")
            if self.handler_failure:
                raise ToolPipelineError("领域 handler 返回限流错误", code="rate_limited")
            self.writes.append(arguments)
            return {"ok": True, "status": "completed", "summary": "已写入"}

        tool = KernelToolSpec(name="test.ref_write", domain="test", description="引用写入",
            input_schema={"type": "object"}, effect=ToolEffect.WRITE,
            prepare=prepare, execute_confirmed=execute,
            verify=lambda _a, _r, _c: not self.verify_failure,
            authorize=lambda _context: not self.denied, metadata={"batch_mode": "sync"})
        self.pipeline = ToolPipeline(catalog=ToolCatalog([tool, replace(tool, name="test.ref_tail", model_name="")]),
            state_store=self.state, reference_store=self.refs,
            effect_store=ConfirmationEffectPlanStore(self.tickets))
        lease, _ = await self.state.begin_turn(owner="ref-owner", session_id="ref-session", request_id="ref-request")

        async def progress(_payload):
            pass

        self.context = ToolCallContext(owner=lease.owner, session_id=lease.session_id,
            request_id=lease.request_id, turn_id=lease.turn_id, lease=lease,
            cancellation=CancellationToken(), report_progress=progress)

    def advance(self, seconds):
        self.ref_time += seconds
        self.ticket_time += seconds

    async def reference(self, *, kind="payload", ttl=900):
        return await self.refs.put(owner="ref-owner", session_id="ref-session", kind=kind,
                                   value={"id": kind}, ttl_seconds=ttl)

    async def pending(self):
        return await self.pipeline.get_pending_plan(owner="ref-owner", session_id="ref-session")

    async def test_remaining_ref_ttl_caps_both_stores_across_different_clock_origins(self):
        for sqlite_refs in (False, True):
            for sqlite_tickets in (False, True):
                with self.subTest(sqlite_refs=sqlite_refs, sqlite_tickets=sqlite_tickets):
                    await self.runtime(sqlite_refs=sqlite_refs, sqlite_tickets=sqlite_tickets)
                    reference = await self.reference()
                    self.advance(850)
                    result = await self.pipeline.execute("test.ref_write", {"payload_ref": reference.ref}, context=self.context)
                    plan = result.effect_plan
                    assert plan is not None
                    self.assertAlmostEqual(plan.expires_at, 2_000_000_900.0, delta=0.02)
                    self.assertAlmostEqual(datetime.fromisoformat(plan.public_dict()["expires_at"]).timestamp(),
                                           plan.expires_at, delta=1)
                    pending = await self.pending()
                    assert pending is not None
                    self.assertLessEqual(pending.expires_at, plan.expires_at)
                    if sqlite_tickets:
                        self.pipeline.effect_store = ConfirmationEffectPlanStore(
                            SQLiteConfirmationStore(clock=lambda: self.ticket_time))
                        assert plan is not None
                        pending_result = await self.pending()
                        assert pending_result is not None
                        self.assertAlmostEqual((pending_result).expires_at, plan.expires_at, delta=0.02)
                    self.assertFalse(self.writes)

    async def test_wall_clock_ref_bounds_monotonic_ticket_with_fractional_remaining_ttl(self):
        await self.runtime(sqlite_refs=True, sqlite_tickets=False)
        self.ref_time, self.ticket_time = 2_000_000_000.0, 1000.0
        reference = await self.reference()
        self.advance(849.5)
        result = await self.pipeline.execute("test.ref_write", {"payload_ref": reference.ref}, context=self.context)
        assert result.effect_plan is not None
        self.assertAlmostEqual(result.effect_plan.expires_at, 1900.0, delta=0.02)
        self.assertEqual(result.effect_plan.public_dict()["expires_at"], "")
        self.advance(50.5)
        self.assertIsNone(await self.pending())
        self.assertFalse(self.writes)

    async def test_expired_input_ref_retires_card_and_never_executes_or_renews(self):
        for sqlite_refs in (False, True):
            with self.subTest(sqlite_refs=sqlite_refs):
                await self.runtime(sqlite_refs=sqlite_refs)
                reference = await self.reference()
                self.advance(850)
                result = await self.pipeline.execute("test.ref_write", {"payload_ref": reference.ref}, context=self.context)
                self.advance(51)
                self.assertIsNone(await self.pending())
                with self.assertRaises(ToolPipelineError) as refused:
                    assert result.effect_plan is not None
                    await self.pipeline.execute_confirmed(result.effect_plan.plan_id, context=self.context)
                self.assertEqual(refused.exception.code, "confirmation_stale")
                self.assertIs(refused.exception.execution_started, False)
                with self.assertRaises(ToolPipelineError) as repeated:
                    assert result.effect_plan is not None
                    await self.pipeline.execute_confirmed(result.effect_plan.plan_id, context=self.context)
                self.assertEqual(repeated.exception.code, "confirmation_invalid")
                self.assertFalse(self.writes)
                with self.assertRaises(ReferenceError):
                    await self.refs.resolve(reference.ref, owner="ref-owner", session_id="ref-session", expected_kind="payload")

    async def test_expired_evidence_survives_card_poll_and_cold_store_but_not_cancellation(self):
        for sqlite_tickets in (False, True):
            with self.subTest(sqlite_tickets=sqlite_tickets):
                await self.runtime(sqlite_refs=True, sqlite_tickets=sqlite_tickets)
                reference = await self.reference(ttl=5)
                preview = await self.pipeline.execute("test.ref_write", {"payload_ref": reference.ref}, context=self.context)
                plan = preview.effect_plan
                self.advance(6)
                self.assertIsNone(await self.pending())
                if sqlite_tickets:
                    self.pipeline.effect_store = ConfirmationEffectPlanStore(SQLiteConfirmationStore(clock=lambda: self.ticket_time))
                evidence = await self.pipeline.get_confirmation_evidence(owner="ref-owner", session_id="ref-session")
                self.assertEqual(evidence, plan)
                self.assertIsNone(await self.pipeline.get_confirmation_evidence(owner="other-owner", session_id="ref-session"))
                # 注销失效证据不能伪造“成功取消了一张有效卡”的历史。
                assert plan is not None
                self.assertFalse(await self.pipeline.cancel_effect(plan.plan_id, lease=self.context.lease))
                self.assertIsNone(await self.pipeline.get_confirmation_evidence(owner="ref-owner", session_id="ref-session"))
                with self.assertRaises(ToolPipelineError) as repeated:
                    await self.pipeline.execute_confirmed(plan.plan_id, context=self.context)
                self.assertEqual(repeated.exception.code, "confirmation_invalid")
                self.assertFalse(self.writes)

    async def test_reference_recovery_ends_at_original_confirmation_window(self):
        for sqlite_tickets in (False, True):
            with self.subTest(sqlite_tickets=sqlite_tickets):
                await self.runtime(sqlite_refs=True, sqlite_tickets=sqlite_tickets)
                started = self.ticket_time
                reference = await self.reference(ttl=5)
                preview = await self.pipeline.execute("test.ref_write", {"payload_ref": reference.ref}, context=self.context)
                plan = preview.effect_plan
                assert plan is not None
                self.assertEqual(plan.confirmation_contract["reference_recovery_until"], started + self.tickets.ttl_seconds)
                self.advance(6)
                self.assertEqual(await self.pipeline.get_confirmation_evidence(owner="ref-owner", session_id="ref-session"), plan)
                self.advance(self.tickets.ttl_seconds - 6)
                # 原行尚在诊断保留期内，但原确认窗口已结束，不能继续启动 Agent。
                assert (self.tickets.inspect_ticket(owner="kernel:ref-owner\x1fref-session", confirmation_id=plan.plan_id)) is not None
                self.assertIsNone(await self.pipeline.get_confirmation_evidence(owner="ref-owner", session_id="ref-session"))
                with self.assertRaises(ToolPipelineError) as expired:
                    await self.pipeline.execute_confirmed(plan.plan_id, context=self.context)
                self.assertEqual(expired.exception.code, "confirmation_invalid")
                self.assertFalse(self.writes)

    async def test_copied_arguments_expire_without_entering_handler_or_authorizing_replay(self):
        for sqlite_tickets in (False, True):
            with self.subTest(sqlite_tickets=sqlite_tickets):
                await self.runtime(sqlite_tickets=sqlite_tickets)
                reference = await self.reference(ttl=5)
                self.frozen_arguments = {"copied": "frozen-value"}
                preview = await self.pipeline.execute("test.ref_write", {"payload_ref": reference.ref}, context=self.context)
                self.advance(6)
                self.assertIsNone(await self.pending())
                with self.assertRaises(ToolPipelineError) as stale:
                    assert preview.effect_plan is not None
                    await self.pipeline.execute_confirmed(preview.effect_plan.plan_id, context=self.context)
                self.assertEqual(stale.exception.code, "confirmation_stale")
                self.assertIs(stale.exception.execution_started, False)
                self.assertFalse(self.writes)
                self.assertIsNone(await self.pipeline.get_confirmation_evidence(owner="ref-owner", session_id="ref-session"))

    async def test_last_valid_second_executes_once_and_consumed_card_stays_dead(self):
        await self.runtime()
        reference = await self.reference()
        self.advance(850)
        result = await self.pipeline.execute("test.ref_write", {"payload_ref": reference.ref}, context=self.context)
        self.advance(49)
        assert result.effect_plan is not None
        completed = await self.pipeline.execute_confirmed(result.effect_plan.plan_id, context=self.context)
        self.assertTrue(completed.outcome.public_content["ok"])
        with self.assertRaises(ToolPipelineError):
            await self.pipeline.execute_confirmed(result.effect_plan.plan_id, context=self.context)
        self.assertEqual(self.writes, [{"payload": {"id": "payload"}}])

    async def test_nested_refs_still_limit_prepared_arguments_that_copy_the_values(self):
        await self.runtime()
        first, second = await self.reference(), await self.reference(kind="target", ttl=875)
        self.advance(850)
        self.frozen_arguments = {"copied": "frozen-value"}
        args = {"groups": [{"payload_ref": first.ref}], "target": {"target_ref": second.ref}}
        result = await self.pipeline.execute("test.ref_write", args, context=self.context)
        assert result.effect_plan is not None
        self.assertEqual(result.effect_plan.arguments, self.frozen_arguments)
        self.assertAlmostEqual(result.effect_plan.expires_at, 2_000_000_875.0, delta=0.02)

    async def test_batch_uses_earliest_input_ref_without_reissuing_it(self):
        await self.runtime()
        first, second = await self.reference(), await self.reference(ttl=875)
        self.advance(850)
        args = {"steps": [{"method": "test.ref_write", "arguments": {"payload_ref": first.ref}},
                          {"method": "test.ref_tail", "arguments": {"payload_ref": second.ref}}]}
        result = await self.pipeline.execute("session.batch", args, context=self.context)
        assert result.effect_plan is not None
        self.assertAlmostEqual(result.effect_plan.expires_at, 2_000_000_875.0, delta=0.02)
        self.assertFalse(self.writes)
        self.advance(26)
        self.assertIsNone(await self.pending())

    async def test_input_expiring_during_prepare_cannot_issue_a_card(self):
        await self.runtime()
        reference = await self.reference()
        self.advance(850)
        self.on_prepare = lambda: self.advance(51)
        with self.assertRaises(ToolPipelineError):
            await self.pipeline.execute("test.ref_write", {"payload_ref": reference.ref}, context=self.context)
        self.assertIsNone(await self.pending())
        self.assertEqual(self.tickets.list_active_tickets(owner="kernel:ref-owner\x1fref-session"), [])
        self.assertFalse(self.writes)

    async def test_expiry_while_issuing_is_a_kernel_rejection_without_a_card(self):
        for sqlite_tickets in (False, True):
            with self.subTest(sqlite_tickets=sqlite_tickets):
                await self.runtime(sqlite_tickets=sqlite_tickets)
                reference = await self.reference()
                self.advance(899.9)

                def delayed_token():
                    time.sleep(0.15)
                    return "ticket-input-expired-during-issue"

                self.tickets._token_factory = delayed_token
                with self.assertRaises(ToolPipelineError) as expired:
                    await self.pipeline.execute("test.ref_write", {"payload_ref": reference.ref}, context=self.context)
                self.assertEqual(expired.exception.code, "reference_invalid")
                self.assertIsNone(await self.pending())
                self.assertEqual(self.tickets.list_active_tickets(owner="kernel:ref-owner\x1fref-session"), [])
                self.assertFalse(self.writes)

    async def test_legacy_plan_without_new_fields_gets_real_ref_cutoff_on_restore(self):
        await self.runtime(sqlite_refs=True)
        reference = await self.reference()
        self.advance(850)
        legacy = self.pipeline.effect_store.freeze(owner="ref-owner", session_id="ref-session",
            generation=self.context.lease.generation, tool_name="test.ref_write", effect=ToolEffect.WRITE,
            arguments={"payload_ref": reference.ref},
            prepared=PreparedEffect(preview={"ok": True}, snapshot_fingerprint="frozen"))
        await self.state.commit(self.context.lease, updates=(StateUpdate("pending_effect_plan_id", legacy.plan_id),))
        self.assertEqual(legacy.expires_at, self.ticket_time + 600)
        pending_result_2 = await self.pending()
        assert pending_result_2 is not None
        self.assertAlmostEqual((pending_result_2).expires_at, 2_000_000_900.0, delta=0.02)
        self.advance(51)
        self.assertIsNone(await self.pending())
        self.assertFalse(self.writes)

    async def test_admission_rejection_is_not_started_and_never_reactivates_the_ticket(self):
        for cause in ("authorization", "rate_limit", "reference"):
            with self.subTest(cause=cause):
                await self.runtime()
                reference = await self.reference()
                self.advance(850)
                result = await self.pipeline.execute("test.ref_write", {"payload_ref": reference.ref}, context=self.context)
                if cause == "authorization":
                    self.denied = True
                elif cause == "rate_limit":
                    async def refuse(**_kwargs):
                        raise ToolPipelineError("执行额度不足", code="rate_limited")
                    self.pipeline.rate_limiter.acquire = refuse
                else:
                    claim = self.pipeline.effect_store.claim
                    def expire_after_claim(_claim=claim, **scope):
                        plan = _claim(**scope)
                        self.advance(51)
                        return plan
                    self.pipeline.effect_store.claim = expire_after_claim
                with self.assertRaises(ToolPipelineError) as rejected:
                    assert result.effect_plan is not None
                    await self.pipeline.execute_confirmed(result.effect_plan.plan_id, context=self.context)
                self.assertIs(getattr(rejected.exception, "execution_started", None), False)
                self.assertIn("本次未执行", str(rejected.exception))
                self.assertIn("重新预检", str(rejected.exception))
                self.assertFalse(self.writes)
                self.assertIsNone(await self.pending())
                with self.assertRaises(ToolPipelineError):
                    assert result.effect_plan is not None
                    await self.pipeline.execute_confirmed(result.effect_plan.plan_id, context=self.context)
                self.assertFalse(self.writes)

    async def test_batch_keeps_written_first_step_when_second_admission_is_rejected(self):
        await self.runtime()
        reference = await self.reference()
        result = await self.pipeline.execute("session.batch", {"steps": [
            {"method": "test.ref_write", "arguments": {"payload_ref": reference.ref}},
            {"method": "test.ref_tail", "arguments": {"payload_ref": reference.ref}},
        ]}, context=self.context)

        async def refuse_tail(**scope):
            if scope["tool_name"] == "confirm:test.ref_tail":
                raise ToolPipelineError("后项执行额度不足", code="rate_limited")

        self.pipeline.rate_limiter.acquire = refuse_tail
        assert result.effect_plan is not None
        completed = await self.pipeline.execute_confirmed(result.effect_plan.plan_id, context=self.context)
        public = completed.outcome.public_content
        first, second = public["data"]["steps"]
        self.assertEqual(public["status"], "partial")
        self.assertEqual(public["data"]["completed"], 1)
        self.assertTrue(first["result"]["ok"])
        self.assertNotIn("本次未执行", first["result"]["summary"])
        self.assertFalse(second["result"]["ok"])
        self.assertIn("本次未执行", second["result"]["summary"])
        self.assertIn("组合变更：1/2 项完成", public["summary"])
        self.assertEqual(self.writes, [{"payload": {"id": "payload"}}])
        self.assertIsNone(await self.pending())
        with self.assertRaises(ToolPipelineError):
            await self.pipeline.execute_confirmed(result.effect_plan.plan_id, context=self.context)
        self.assertEqual(len(self.writes), 1)

    async def test_handler_and_verify_failure_are_not_mislabeled_as_not_started(self):
        for phase in ("handler", "verify"):
            with self.subTest(phase=phase):
                await self.runtime()
                reference = await self.reference()
                self.advance(850)
                result = await self.pipeline.execute("test.ref_write", {"payload_ref": reference.ref}, context=self.context)
                self.handler_failure, self.verify_failure = phase == "handler", phase == "verify"
                with self.assertRaises(ToolPipelineError) as failed:
                    assert result.effect_plan is not None
                    await self.pipeline.execute_confirmed(result.effect_plan.plan_id, context=self.context)
                self.assertIs(getattr(failed.exception, "execution_started", None), True)
                self.assertNotIn("本次未执行", str(failed.exception))
                self.assertEqual(len(self.writes), int(phase == "verify"))
                with self.assertRaises(ToolPipelineError):
                    assert result.effect_plan is not None
                    await self.pipeline.execute_confirmed(result.effect_plan.plan_id, context=self.context)
                self.assertEqual(len(self.writes), int(phase == "verify"))
