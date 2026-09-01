"""Agent 受控写操作确认协议与 STRM 首个动作测试。"""
from __future__ import annotations

import re
import threading
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app.agent.confirmation import ConfirmationStore, confirmation_reply_intent
from app.agent.confirmation_contract import (
    build_confirmation_contract,
    sanitize_confirmation_contract,
)
from app.agent.feature_gate import AgentRuntimeDisabled
from app.agent.models import RiskLevel, ToolResult, ToolSpec
from app.agent.orchestrator import (
    AgentOrchestrator,
    _QUERY_CONFIRMATION_EPOCH,
    _is_strm_run_action,
)
from app.agent.registry import AgentToolError, ToolRegistry
from app.agent.service import get_agent_service, reset_agent_service_for_tests
from app.agent.tools import (
    build_tool_registry,
    prepare_strm_run_once,
    run_strm_once_confirmed,
)
from app.agent.rate_limit import agent_rate_limiter
from app.main import create_app
from tests.support import IsolatedDatabaseTestCase


class ConfirmationStoreTests(unittest.TestCase):
    def test_expected_owner_generation_rejects_bool_even_when_epoch_is_one(self):
        store = ConfirmationStore(
            token_factory=lambda: "ticket-generation-bool-1234"
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
        store = ConfirmationStore(
            token_factory=lambda: "ticket-strict-plan-id-123456"
        )
        ticket = store.issue(
            owner="owner-a", tool_name="write.test", arguments={"id": 1}
        )

        class StringLikePlanId:
            def __str__(self) -> str:
                return ticket.confirmation_id

        with self.assertRaises(AgentToolError) as invalid:
            store.claim_and_rotate_owner(
                owner="owner-a",
                confirmation_id=StringLikePlanId(),  # type: ignore[arg-type]
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
        store = ConfirmationStore(
            token_factory=lambda: "ticket-audit-order-123456"
        )
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
            ) as record_claimed,
        ):
            with self.assertRaises(AgentToolError) as unavailable:
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
        ticket = store.issue(owner="owner-a", tool_name="write.test", arguments={"id": 1})

        first = store.list_active_tickets(owner="owner-a")
        second = store.list_active_tickets(owner="owner-a")

        self.assertEqual([item.confirmation_id for item in first], [ticket.confirmation_id])
        self.assertEqual([item.confirmation_id for item in second], [ticket.confirmation_id])
        self.assertEqual(store.claim_and_rotate_owner(owner="owner-a", confirmation_id=ticket.confirmation_id).arguments, {"id": 1})

    def test_claim_and_rotate_revokes_same_owner_but_not_other_owner(self):
        tokens = iter((
            "ticket-owner-a-first-123456",
            "ticket-owner-a-second-12345",
            "ticket-owner-b-first-123456",
        ))
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

        claimed = store.claim_and_rotate_owner(owner="owner-a", confirmation_id=ticket.confirmation_id)
        self.assertEqual(claimed.arguments, {"items": ["one"]})
        self.assertEqual(claimed.followup_context, {"verification": {"episode": 3}})
        self.assertEqual(claimed.confirmation_contract["object"], "候选资源")
        claimed.followup_context["verification"]["episode"] = 5
        claimed.confirmation_contract["impact"] = "已被领取方修改"
        self.assertEqual(ticket.followup_context, {"verification": {"episode": 3}})
        self.assertEqual(ticket.confirmation_contract["impact"], "会创建下载任务")
        with self.assertRaises(AgentToolError) as replay:
            store.claim_and_rotate_owner(owner="owner-a", confirmation_id=ticket.confirmation_id)
        self.assertEqual(replay.exception.code, "confirmation_invalid")

    def test_returned_ticket_cannot_mutate_pending_execution_payload(self):
        store = ConfirmationStore(
            token_factory=lambda: "ticket-detached-return-123456"
        )
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
                        owner="owner-b",
                        tool_name="write.test",
                        **kwargs,  # type: ignore[arg-type]
                    )
                self.assertEqual(invalid.exception.code, "confirmation_invalid")
                self.assertEqual(token_factory.call_count, 0)
                self.assertEqual(
                    [item.confirmation_id for item in store.list_active_tickets(owner="owner-a")],
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
            store.claim_and_rotate_owner(owner="owner-b", confirmation_id=ticket.confirmation_id)
        claimed = store.claim_and_rotate_owner(owner="owner-a", confirmation_id=ticket.confirmation_id)
        self.assertEqual(claimed.owner, "owner-a")

        ticket = store.issue(owner="owner-a", tool_name="write.test", arguments={})
        now[0] += 10
        with self.assertRaises(AgentToolError) as expired:
            store.claim_and_rotate_owner(owner="owner-a", confirmation_id=ticket.confirmation_id)
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
        store = ConfirmationStore(
            max_entries=1,
            token_factory=lambda: next(tokens),
        )
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
        tokens = iter((
            "ticket-capacity-owner-a-old-1",
            "ticket-capacity-owner-a-new-1",
        ))
        ticks = iter((100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0))
        store = ConfirmationStore(
            max_entries=1,
            clock=lambda: next(ticks),
            token_factory=lambda: next(tokens),
        )
        previous = store.issue(
            owner="owner-a", tool_name="write.test", arguments={"id": "old"}
        )
        current = store.issue(
            owner="owner-a", tool_name="write.test", arguments={"id": "new"}
        )

        self.assertEqual(len(store.list_active_tickets(owner="owner-a")), 1)
        with self.assertRaises(AgentToolError):
            store.claim_and_rotate_owner(owner="owner-a", confirmation_id=previous.confirmation_id)
        self.assertEqual(
            store.claim_and_rotate_owner(
                owner="owner-a", confirmation_id=current.confirmation_id
            ).arguments,
            {"id": "new"},
        )

    def test_replacing_plan_at_capacity_does_not_evict_other_owner(self):
        tokens = iter((
            "ticket-capacity-owner-b-1234",
            "ticket-capacity-owner-a-old-1",
            "ticket-capacity-owner-a-new-1",
        ))
        now = iter((100.0, 101.0, 102.0, 103.0, 104.0, 105.0))
        store = ConfirmationStore(
            max_entries=2,
            clock=lambda: next(now),
            token_factory=lambda: next(tokens),
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
            store.claim_and_rotate_owner(owner="owner-b", confirmation_id=other.confirmation_id).arguments,
            {"id": "b"},
        )
        with self.assertRaises(AgentToolError):
            store.claim_and_rotate_owner(owner="owner-a", confirmation_id=previous.confirmation_id)
        self.assertEqual(
            store.claim_and_rotate_owner(
                owner="owner-a", confirmation_id=replacement.confirmation_id
            ).arguments,
            {"id": "new"},
        )

    def test_rotate_owner_can_preserve_active_ticket_under_new_epoch(self):
        store = ConfirmationStore(
            token_factory=lambda: "ticket-preserved-query-123456"
        )
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
        self.assertEqual([item.confirmation_id for item in active], [ticket.confirmation_id])
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
            store.claim_and_rotate_owner(owner="owner-a", confirmation_id=ticket.confirmation_id)
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
                store.claim_and_rotate_owner(owner="owner", confirmation_id=ticket.confirmation_id)
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
        tokens = iter((
            "ticket-owner-a-00000001",
            "ticket-owner-a-00000002",
            "ticket-owner-b-00000001",
        ))
        store = ConfirmationStore(token_factory=lambda: next(tokens))
        first = store.issue(owner="owner-a", tool_name="write.test", arguments={})
        second = store.issue(owner="owner-a", tool_name="write.test", arguments={})
        other = store.issue(owner="owner-b", tool_name="write.test", arguments={})

        self.assertFalse(store.discard(owner="owner-b", confirmation_id=first.confirmation_id))
        self.assertTrue(store.discard(owner="owner-a", confirmation_id=first.confirmation_id))
        self.assertEqual(store.revoke_owner(owner="owner-a"), 1)
        with self.assertRaises(AgentToolError):
            store.claim_and_rotate_owner(owner="owner-a", confirmation_id=second.confirmation_id)
        self.assertEqual(
            store.claim_and_rotate_owner(owner="owner-b", confirmation_id=other.confirmation_id).owner,
            "owner-b",
        )


class ConfirmedToolRegistryTests(unittest.TestCase):
    def test_registry_rejects_duplicate_direct_handler_modes(self):
        registry = ToolRegistry()
        with self.assertRaisesRegex(ValueError, "duplicate direct handler modes"):
            registry.register(ToolSpec(
                name="read.duplicate-handler-modes",
                description="test",
                risk=RiskLevel.READ,
                parameters={},
                validator=lambda arguments: dict(arguments),
                handler=lambda _arguments: ToolResult(True, "completed", "plain"),
                context_handler=lambda _arguments, _context: ToolResult(
                    True, "completed", "context"
                ),
            ))

    def test_registry_requires_canonical_preparer_for_confirmation_tool(self):
        registry = ToolRegistry()
        with self.assertRaisesRegex(ValueError, "requires preparer"):
            registry.register(ToolSpec(
                name="write.incomplete-preparer",
                description="test",
                risk=RiskLevel.WRITE,
                parameters={},
                validator=lambda arguments: dict(arguments),
                requires_confirmation=True,
                confirmed_handler=lambda _arguments, _context: ToolResult(
                    True, "completed", "done"
                ),
            ))

    def test_registry_requires_explicit_confirmed_handler_for_canonical_preparer(self):
        registry = ToolRegistry()
        with self.assertRaisesRegex(ValueError, "requires confirmed handler"):
            registry.register(ToolSpec(
                name="write.incomplete",
                description="test",
                risk=RiskLevel.WRITE,
                parameters={},
                validator=lambda arguments: dict(arguments),
                requires_confirmation=True,
                confirmation_preparer=lambda _arguments: (
                    ToolResult(True, "confirmation_required", "preview"),
                    "fingerprint",
                ),
            ))

    def test_confirmation_preparers_reject_invalid_outputs_without_issuing_ticket(self):
        preview = ToolResult(True, "confirmation_required", "preview")
        cases = (
            ("plain_result", False, (object(), "fingerprint")),
            ("plain_fingerprint", False, (preview, object())),
            ("context_result", True, (object(), "fingerprint")),
            ("context_fingerprint", True, (preview, object())),
        )
        for name, contextual, prepared_output in cases:
            with self.subTest(name=name):
                registry = ToolRegistry()
                handler_kwargs = (
                    {
                        "context_confirmation_preparer": (
                            lambda _arguments, _context, output=prepared_output: output
                        ),
                        "context_confirmed_handler": (
                            lambda _arguments, _fingerprint, _context: ToolResult(
                                True, "completed", "done"
                            )
                        ),
                    }
                    if contextual
                    else {
                        "confirmation_preparer": (
                            lambda _arguments, output=prepared_output: output
                        ),
                        "confirmed_handler": (
                            lambda _arguments, _fingerprint: ToolResult(
                                True, "completed", "done"
                            )
                        ),
                    }
                )
                registry.register(ToolSpec(
                    name=f"write.invalid-preparer-{name}",
                    description="test",
                    risk=RiskLevel.WRITE,
                    parameters={},
                    validator=lambda _arguments: {},
                    requires_confirmation=True,
                    **handler_kwargs,
                ))
                service = AgentOrchestrator(
                    registry,
                    ConfirmationStore(
                        token_factory=lambda: "ticket-invalid-preparer-1234"
                    ),
                )

                with self.assertRaises(AgentToolError) as unavailable:
                    service.prepare(
                        f"write.invalid-preparer-{name}", {}, owner="owner-a"
                    )

                self.assertEqual(unavailable.exception.code, "confirmation_unavailable")
                self.assertEqual(
                    service.confirmation_store.list_active_tickets(owner="owner-a"),
                    [],
                )

    def test_invalid_confirmed_handler_result_fails_closed_after_single_claim(self):
        for contextual in (False, True):
            with self.subTest(contextual=contextual):
                registry = ToolRegistry()
                handler_kwargs = (
                    {
                        "context_confirmation_preparer": (
                            lambda _arguments, _context: (
                                ToolResult(True, "confirmation_required", "preview"),
                                "context-fingerprint",
                            )
                        ),
                        "context_confirmed_handler": (
                            lambda _arguments, _fingerprint, _context: {"ok": True}
                        ),
                    }
                    if contextual
                    else {
                        "confirmation_preparer": lambda _arguments: (
                            ToolResult(True, "confirmation_required", "preview"),
                            "context-fingerprint",
                        ),
                        "confirmed_handler": (
                            lambda _arguments, _fingerprint: {"ok": True}
                        ),
                    }
                )
                registry.register(ToolSpec(
                    name=f"write.invalid-confirmed-{int(contextual)}",
                    description="test",
                    risk=RiskLevel.WRITE,
                    parameters={},
                    validator=lambda _arguments: {},
                    requires_confirmation=True,
                    **handler_kwargs,
                ))
                service = AgentOrchestrator(
                    registry,
                    ConfirmationStore(
                        token_factory=lambda: "ticket-invalid-confirmed-1234"
                    ),
                )
                prepared = service.prepare(
                    f"write.invalid-confirmed-{int(contextual)}",
                    {},
                    owner="owner-a",
                )

                response = service.confirm(
                    prepared["action_plan"]["plan_id"], owner="owner-a"
                )

                self.assertFalse(response["result"]["ok"])
                self.assertEqual(response["result"]["status"], "unavailable")
                self.assertEqual(response["action_plan"]["status"], "failed")
                with self.assertRaises(AgentToolError):
                    service.confirm(
                        prepared["action_plan"]["plan_id"], owner="owner-a"
                    )

    def test_orchestrator_prepare_returns_stable_human_action_plan(self):
        registry = ToolRegistry()
        registry.register(ToolSpec(
            name="config.set_feature_state",
            description="test",
            risk=RiskLevel.WRITE,
            parameters={},
            validator=lambda arguments: {
                "feature": str(arguments.get("feature") or "discovery"),
                "enabled": bool(arguments.get("enabled")),
            },
            confirmation_preparer=lambda _arguments: (
                ToolResult(
                    True,
                    "confirmation_required",
                    "预检通过：保存后会更新功能状态。",
                ),
                "feature-state",
            ),
            confirmed_handler=lambda _arguments, _context: ToolResult(
                True, "completed", "done"
            ),
            requires_confirmation=True,
        ))
        service = AgentOrchestrator(
            registry,
            ConfirmationStore(token_factory=lambda: "ticket-contract-123456789"),
        )

        response = service.prepare(
            "config.set_feature_state",
            {"feature": "discovery", "enabled": True},
            owner="owner-a",
        )

        self.assertEqual(response["mode"], "confirmation_required")
        self.assertNotIn("confirmation", response)
        plan = response["action_plan"]
        self.assertEqual(plan["version"], 1)
        self.assertEqual(plan["title"], "切换项目功能状态")
        self.assertEqual(plan["target"], "本次预检指向的功能")
        self.assertIn("改变该功能的可用状态", plan["impact"])
        self.assertIn("再次切换回原状态", plan["reversibility"])
        self.assertEqual(plan["risk"], "write")
        self.assertEqual(plan["preflight_summary"], "预检通过:保存后会更新功能状态。")
        self.assertRegex(
            plan["preflight_at"],
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$",
        )

    def test_prepare_and_confirm_bind_normalized_arguments_and_context(self):
        context = {"value": "one"}
        calls: list[dict] = []
        registry = ToolRegistry()
        def confirmed(arguments, expected_context):
            if context["value"] != expected_context:
                raise AgentToolError("stale", code="confirmation_stale")
            calls.append(dict(arguments))
            return ToolResult(True, "accepted", "done")

        registry.register(ToolSpec(
            name="write.test",
            description="test",
            risk=RiskLevel.WRITE,
            parameters={},
            validator=lambda arguments: {"value": str(arguments.get("value", "")).strip()},
            confirmation_preparer=lambda arguments: (
                ToolResult(
                    True, "confirmation_required", "preview", data=arguments
                ),
                context["value"],
            ),
            confirmed_handler=confirmed,
            requires_confirmation=True,
        ))

        spec, arguments, fingerprint, preview, _elapsed = registry.prepare_confirmation(
            "write.test", {"value": " x "}
        )
        self.assertEqual(spec.risk, RiskLevel.WRITE)
        self.assertEqual(arguments, {"value": "x"})
        self.assertEqual(fingerprint, "one")
        self.assertEqual(preview.data, {"value": "x"})
        result, _elapsed = registry.execute_confirmed(
            "write.test", arguments, expected_context=fingerprint
        )
        self.assertTrue(result.ok)
        self.assertEqual(calls, [{"value": "x"}])

        context["value"] = "two"
        with self.assertRaises(AgentToolError) as stale:
            registry.execute_confirmed("write.test", arguments, expected_context=fingerprint)
        self.assertEqual(stale.exception.code, "confirmation_stale")

    def test_post_write_verifier_receives_normalized_arguments_after_success(self):
        calls: list[tuple[dict, str]] = []
        registry = ToolRegistry()

        def verifier(arguments, result):
            calls.append((dict(arguments), result.summary))
            data = dict(result.data)
            data["verification_state"] = "verified"
            return ToolResult(True, result.status, result.summary, data=data)

        registry.register(ToolSpec(
            name="write.verify",
            description="test",
            risk=RiskLevel.WRITE,
            parameters={},
            validator=lambda arguments: {"value": str(arguments.get("value", "")).strip()},
            confirmation_preparer=lambda _arguments: (
                ToolResult(True, "preview", "preview"), "verify"
            ),
            confirmed_handler=lambda arguments, _context: ToolResult(
                True, "completed", "done", data={"value": arguments["value"]}
            ),
            requires_confirmation=True,
            post_write_verifier=verifier,
        ))

        result, _elapsed = registry.execute_confirmed(
            "write.verify", {"value": " x "}
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.data["verification_state"], "verified")
        self.assertEqual(calls, [({"value": "x"}, "done")])

    def test_post_write_verifier_failure_preserves_completed_write(self):
        registry = ToolRegistry()
        registry.register(ToolSpec(
            name="write.verify_failure",
            description="test",
            risk=RiskLevel.WRITE,
            parameters={},
            validator=lambda _arguments: {},
            confirmation_preparer=lambda _arguments: (
                ToolResult(True, "preview", "preview"), "verify-failure"
            ),
            confirmed_handler=lambda _arguments, _context: ToolResult(
                True, "completed", "write completed"
            ),
            requires_confirmation=True,
            post_write_verifier=lambda _arguments, _result: (_ for _ in ()).throw(
                OSError("must-not-leak")
            ),
        ))

        result, _elapsed = registry.execute_confirmed("write.verify_failure", {})

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.summary, "write completed")
        self.assertEqual(result.data["verification_state"], "pending")
        self.assertIn("回读验证", " ".join(result.suggestions))
        self.assertNotIn("must-not-leak", str(result.to_dict()))

    def test_confirmed_handler_receives_bound_context_and_propagates_stale(self):
        calls = []
        registry = ToolRegistry()

        def confirmed(arguments, expected_context):
            calls.append((dict(arguments), expected_context))
            if expected_context != "bound-context":
                raise AgentToolError("stale", code="confirmation_stale")
            return ToolResult(True, "accepted", "done")

        registry.register(ToolSpec(
            name="write.confirmed",
            description="test",
            risk=RiskLevel.DANGER,
            parameters={},
            validator=lambda _arguments: {},
            confirmation_preparer=lambda _arguments: (
                ToolResult(True, "preview", "preview"), "bound-context"
            ),
            confirmed_handler=confirmed,
            requires_confirmation=True,
        ))

        result, _elapsed = registry.execute_confirmed(
            "write.confirmed", {}, expected_context="bound-context"
        )
        self.assertTrue(result.ok)
        self.assertEqual(calls, [({}, "bound-context")])

        with self.assertRaises(AgentToolError) as stale:
            confirmed({}, "other-context")
        self.assertEqual(stale.exception.code, "confirmation_stale")

    def test_orchestrator_reset_during_preview_does_not_leave_live_ticket(self):
        preview_started = threading.Event()
        release_preview = threading.Event()
        registry = ToolRegistry()

        def prepare_confirmation(_arguments):
            preview_started.set()
            self.assertTrue(release_preview.wait(timeout=2))
            return ToolResult(True, "confirmation_required", "preview"), "race"

        registry.register(ToolSpec(
            name="write.race",
            description="test",
            risk=RiskLevel.WRITE,
            parameters={},
            validator=lambda _arguments: {},
            confirmation_preparer=prepare_confirmation,
            confirmed_handler=lambda _arguments, _context: ToolResult(
                True, "accepted", "done"
            ),
            requires_confirmation=True,
        ))
        service = AgentOrchestrator(
            registry,
            ConfirmationStore(token_factory=lambda: "ticket-race-1234567890"),
        )
        outcome: list[object] = []

        def prepare():
            try:
                outcome.append(service.prepare("write.race", {}, owner="owner-a"))
            except Exception as exc:  # noqa: BLE001 - 断言竞态失败类型
                outcome.append(exc)

        worker = threading.Thread(target=prepare)
        worker.start()
        self.assertTrue(preview_started.wait(timeout=2))
        service.reset_session(owner="owner-a")
        release_preview.set()
        worker.join(timeout=2)

        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], AgentToolError)
        self.assertEqual(outcome[0].code, "confirmation_invalid")

    def test_cancelled_query_epoch_cannot_issue_ticket_after_preview(self):
        preview_started = threading.Event()
        release_preview = threading.Event()
        registry = ToolRegistry()

        def prepare_confirmation(_arguments):
            preview_started.set()
            self.assertTrue(release_preview.wait(timeout=2))
            return ToolResult(True, "confirmation_required", "preview"), "cancelled"

        registry.register(ToolSpec(
            name="write.cancelled-query",
            description="test",
            risk=RiskLevel.WRITE,
            parameters={},
            validator=lambda _arguments: {},
            confirmation_preparer=prepare_confirmation,
            confirmed_handler=lambda _arguments, _context: ToolResult(
                True, "accepted", "done"
            ),
            requires_confirmation=True,
        ))
        service = AgentOrchestrator(
            registry,
            ConfirmationStore(token_factory=lambda: "ticket-cancelled-query-1234"),
        )
        epoch = service.begin_query_confirmation_epoch(owner="owner-a")
        outcome: list[object] = []

        def prepare() -> None:
            try:
                outcome.append(service.prepare(
                    "write.cancelled-query",
                    {},
                    owner="owner-a",
                    expected_owner_generation=epoch,
                ))
            except Exception as exc:  # noqa: BLE001 - 断言竞态失败类型
                outcome.append(exc)

        worker = threading.Thread(target=prepare)
        worker.start()
        self.assertTrue(preview_started.wait(timeout=2))
        service.invalidate_query_confirmation_epoch(owner="owner-a")
        release_preview.set()
        worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], AgentToolError)
        self.assertEqual(outcome[0].code, "confirmation_invalid")

    def test_confirm_and_discard_advance_epoch_against_late_prepare(self):
        registry = ToolRegistry()
        registry.register(ToolSpec(
            name="write.epoch",
            description="test",
            risk=RiskLevel.WRITE,
            parameters={},
            validator=lambda _arguments: {},
            confirmation_preparer=lambda _arguments: (
                ToolResult(True, "confirmation_required", "preview"), "epoch"
            ),
            confirmed_handler=lambda _arguments, _context: ToolResult(
                True, "accepted", "done"
            ),
            requires_confirmation=True,
        ))
        service = AgentOrchestrator(
            registry,
            ConfirmationStore(token_factory=lambda: "ticket-epoch-advance-1234"),
        )

        confirm_epoch = service.begin_query_confirmation_epoch(owner="owner-a")
        prepared = service.prepare(
            "write.epoch",
            {},
            owner="owner-a",
            expected_owner_generation=confirm_epoch,
        )
        service.confirm(
            prepared["action_plan"]["plan_id"], owner="owner-a"
        )
        with self.assertRaises(AgentToolError) as late_confirm:
            service.prepare(
                "write.epoch",
                {},
                owner="owner-a",
                expected_owner_generation=confirm_epoch,
            )
        self.assertEqual(late_confirm.exception.code, "confirmation_invalid")

        discard_epoch = service.begin_query_confirmation_epoch(owner="owner-a")
        prepared = service.prepare(
            "write.epoch",
            {},
            owner="owner-a",
            expected_owner_generation=discard_epoch,
        )
        self.assertTrue(service.discard_confirmation(
            prepared["action_plan"]["plan_id"], owner="owner-a"
        ))
        with self.assertRaises(AgentToolError) as late_discard:
            service.prepare(
                "write.epoch",
                {},
                owner="owner-a",
                expected_owner_generation=discard_epoch,
            )
        self.assertEqual(late_discard.exception.code, "confirmation_invalid")

    def test_orchestrator_rejects_coercible_confirmation_generations(self):
        registry = ToolRegistry()
        registry.register(ToolSpec(
            name="write.strict-epoch",
            description="test",
            risk=RiskLevel.WRITE,
            parameters={},
            validator=lambda _arguments: {},
            confirmation_preparer=lambda _arguments: (
                ToolResult(True, "confirmation_required", "preview"), "epoch"
            ),
            confirmed_handler=lambda _arguments, _context: ToolResult(
                True, "accepted", "done"
            ),
            requires_confirmation=True,
        ))
        service = AgentOrchestrator(
            registry,
            ConfirmationStore(token_factory=lambda: "ticket-strict-epoch-1234"),
        )

        for generation in (True, "1", 1.0, 0, -1):
            with self.subTest(prepare_generation=generation):
                with self.assertRaises(AgentToolError) as invalid:
                    service.prepare(
                        "write.strict-epoch",
                        {},
                        owner="owner-a",
                        expected_owner_generation=generation,
                    )
                self.assertEqual(invalid.exception.code, "confirmation_invalid")

            with self.subTest(query_generation=generation):
                with self.assertRaises(AgentToolError) as invalid:
                    service.query(
                        "你好",
                        owner="owner-a",
                        confirmation_owner_generation=generation,
                    )
                self.assertEqual(invalid.exception.code, "confirmation_invalid")

        self.assertEqual(
            service.confirmation_store.list_active_tickets(owner="owner-a"), []
        )

    def test_unpublished_plan_cleanup_preserves_newer_query_epoch(self):
        tokens = iter((
            "ticket-hidden-plan-old-1234",
            "ticket-hidden-plan-new-1234",
        ))
        registry = ToolRegistry()
        registry.register(ToolSpec(
            name="write.hidden-plan",
            description="test",
            risk=RiskLevel.WRITE,
            parameters={},
            validator=lambda _arguments: {},
            confirmation_preparer=lambda _arguments: (
                ToolResult(True, "confirmation_required", "preview"), "epoch"
            ),
            confirmed_handler=lambda _arguments, _context: ToolResult(
                True, "accepted", "done"
            ),
            requires_confirmation=True,
        ))
        service = AgentOrchestrator(
            registry, ConfirmationStore(token_factory=lambda: next(tokens))
        )

        old_epoch = service.begin_query_confirmation_epoch(owner="owner-a")
        hidden = service.prepare(
            "write.hidden-plan",
            {},
            owner="owner-a",
            expected_owner_generation=old_epoch,
        )
        newer_epoch = service.begin_query_confirmation_epoch(owner="owner-a")

        self.assertTrue(service.discard_confirmation(
            hidden["action_plan"]["plan_id"],
            owner="owner-a",
            advance_owner_epoch=False,
        ))
        replacement = service.prepare(
            "write.hidden-plan",
            {},
            owner="owner-a",
            expected_owner_generation=newer_epoch,
        )

        self.assertEqual(
            service.confirmation_store.list_active_tickets(owner="owner-a")[0]
            .confirmation_id,
            replacement["action_plan"]["plan_id"],
        )

    def test_new_action_plan_atomically_supersedes_previous_plan(self):
        tokens = iter((
            "ticket-single-plan-first-1234",
            "ticket-single-plan-second-123",
        ))
        registry = ToolRegistry()
        registry.register(ToolSpec(
            name="write.single-plan",
            description="test",
            risk=RiskLevel.WRITE,
            parameters={
                "type": "object",
                "required": ["value"],
                "properties": {"value": {"type": "string"}},
                "additionalProperties": False,
            },
            validator=lambda arguments: {"value": str(arguments["value"])},
            confirmation_preparer=lambda arguments: (
                ToolResult(
                    True, "confirmation_required", f"preview {arguments['value']}"
                ),
                f"single-plan:{arguments['value']}",
            ),
            confirmed_handler=lambda _arguments, _context: ToolResult(
                True, "accepted", "done"
            ),
            requires_confirmation=True,
        ))
        service = AgentOrchestrator(
            registry, ConfirmationStore(token_factory=lambda: next(tokens))
        )
        first = service.prepare(
            "write.single-plan", {"value": "old"}, owner="owner-a"
        )
        replacement = service.prepare(
            "write.single-plan", {"value": "new"}, owner="owner-a"
        )

        with self.assertRaises(AgentToolError):
            service.confirm(first["action_plan"]["plan_id"], owner="owner-a")
        active = service.confirmation_store.list_active_tickets(owner="owner-a")
        self.assertEqual(
            [item.confirmation_id for item in active],
            [replacement["action_plan"]["plan_id"]],
        )
        self.assertEqual(active[0].arguments, {"value": "new"})

    def test_current_query_can_cancel_then_prepare_replacement_plan(self):
        tokens = iter((
            "ticket-replace-plan-first-123",
            "ticket-replace-plan-second-12",
        ))
        registry = ToolRegistry()
        registry.register(ToolSpec(
            name="write.replace-plan",
            description="test",
            risk=RiskLevel.WRITE,
            parameters={
                "type": "object",
                "required": ["value"],
                "properties": {"value": {"type": "string"}},
                "additionalProperties": False,
            },
            validator=lambda arguments: {"value": str(arguments["value"])},
            confirmation_preparer=lambda arguments: (
                ToolResult(
                    True, "confirmation_required", f"preview {arguments['value']}"
                ),
                f"replacement-plan:{arguments['value']}",
            ),
            confirmed_handler=lambda _arguments, _context: ToolResult(
                True, "accepted", "done"
            ),
            requires_confirmation=True,
        ))
        service = AgentOrchestrator(
            registry, ConfirmationStore(token_factory=lambda: next(tokens))
        )
        first_epoch = service.begin_query_confirmation_epoch(owner="owner-a")
        first = service.prepare(
            "write.replace-plan",
            {"value": "old"},
            owner="owner-a",
            expected_owner_generation=first_epoch,
        )
        replacement_epoch = service.begin_query_confirmation_epoch(owner="owner-a")
        token = _QUERY_CONFIRMATION_EPOCH.set(("owner-a", replacement_epoch))
        try:
            self.assertTrue(service.discard_confirmation(
                first["action_plan"]["plan_id"], owner="owner-a"
            ))
            replacement = service.prepare(
                "write.replace-plan", {"value": "new"}, owner="owner-a"
            )
        finally:
            _QUERY_CONFIRMATION_EPOCH.reset(token)

        self.assertNotEqual(
            replacement["action_plan"]["plan_id"],
            first["action_plan"]["plan_id"],
        )
        active = service.confirmation_store.list_active_tickets(owner="owner-a")
        self.assertEqual(
            [item.confirmation_id for item in active],
            [replacement["action_plan"]["plan_id"]],
        )

    def test_orchestrator_consumes_ticket_before_failed_handler(self):
        calls = Mock()
        registry = ToolRegistry()
        def failed_handler(_arguments, _expected_context):
            calls()
            return ToolResult(False, "conflict", "busy")

        registry.register(ToolSpec(
            name="strm.run_once",
            description="test",
            risk=RiskLevel.DANGER,
            parameters={},
            validator=lambda _arguments: {},
            confirmation_preparer=lambda _arguments: (
                ToolResult(True, "confirmation_required", "preview"), "failed"
            ),
            confirmed_handler=failed_handler,
            requires_confirmation=True,
        ))
        service = AgentOrchestrator(
            registry,
            ConfirmationStore(token_factory=lambda: "ticket-failed-123456789"),
        )
        prepared = service.query("立即执行 STRM 同步", owner="owner")
        confirmation_id = prepared["action_plan"]["plan_id"]
        response = service.confirm(confirmation_id, owner="owner")
        self.assertEqual(response["result"]["status"], "conflict")
        with self.assertRaises(AgentToolError):
            service.confirm(confirmation_id, owner="owner")
        calls.assert_called_once_with()


class StrmRunIntentTests(unittest.TestCase):
    def test_run_intent_requires_an_explicit_affirmative_command(self):
        self.assertTrue(_is_strm_run_action("立即执行 STRM 同步"))
        for message in (
            "不要立即执行 STRM 同步",
            "不用启动 STRM 同步",
            "能否立即执行 STRM 同步",
            "STRM 同步可以立即开始吗",
            "如何立即执行 STRM 同步？",
            "如果立即执行 STRM 同步会怎样",
            "查看 STRM 同步状态",
        ):
            self.assertFalse(_is_strm_run_action(message), message)


class StrmConfirmedToolTests(unittest.TestCase):
    def test_preview_does_not_trigger_and_execution_is_fixed_to_manual(self):
        scheduler = Mock()
        scheduler.validate_config.return_value = ""
        scheduler.status.return_value = {"running": False, "sources": [{"id": "secret-source"}]}
        scheduler.trigger.return_value = {"ok": True, "message": "raw"}
        with patch("app.modules.scheduler.get_scheduler", return_value=scheduler):
            preview, context = prepare_strm_run_once({})
            result = run_strm_once_confirmed({}, context)
        self.assertTrue(preview.ok)
        self.assertNotIn("secret-source", str(preview.to_dict()))
        self.assertEqual(result.data, {"accepted": True, "trigger": "manual"})
        scheduler.trigger.assert_called_once_with("manual")

    def test_invalid_config_and_busy_result_are_sanitized(self):
        scheduler = Mock()
        scheduler.validate_config.return_value = "raw /secret/path config error"
        with patch("app.modules.scheduler.get_scheduler", return_value=scheduler):
            preview, _context = prepare_strm_run_once({})
        self.assertFalse(preview.ok)
        self.assertNotIn("/secret/path", str(preview.to_dict()))
        scheduler.trigger.assert_not_called()

        scheduler.validate_config.return_value = ""
        scheduler.trigger.return_value = {"ok": False, "error": "raw /secret/path lock error"}
        with patch("app.modules.scheduler.get_scheduler", return_value=scheduler):
            _preview, context = prepare_strm_run_once({})
            result = run_strm_once_confirmed({}, context)
        self.assertEqual(result.status, "conflict")
        self.assertNotIn("/secret/path", str(result.to_dict()))


class AgentConfirmedActionAPITests(IsolatedDatabaseTestCase):
    def setUp(self):
        reset_agent_service_for_tests()
        agent_rate_limiter.reset()
        self.client = TestClient(create_app(start_background=False), raise_server_exceptions=False)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        reset_agent_service_for_tests()
        agent_rate_limiter.reset()

    @staticmethod
    def _token(html: str) -> str:
        match = re.search(r'name="csrf_token"\s+(?:value|content)="([^"]+)"', html)
        if not match:
            match = re.search(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', html)
        if not match:
            raise AssertionError("CSRF token missing")
        return match.group(1)

    def login(self) -> str:
        token = self._token(self.client.get("/login").text)
        response = self.client.post(
            "/login",
            data={"username": "admin", "password": "123456", "csrf_token": token},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302, response.text)
        return self._token(self.client.get("/settings").text)

    @staticmethod
    def _config_get(values):
        return lambda key, default="": values.get(key, default)

    def test_query_prepare_confirm_and_replay(self):
        csrf = self.login()
        scheduler = Mock()
        scheduler.validate_config.return_value = ""
        scheduler.status.return_value = {"running": False}
        scheduler.trigger.return_value = {"ok": True, "message": "raw /private/path"}
        values = {
            "AGENT_ENABLED": "1",
            "GY_STRM_SOURCE_DIRS": '[{"id":"secret-source","name":"Secret"}]',
            "GY_STRM_BASE_URL": "http://private-service",
            "STRM_ROOT": "/private/root",
        }
        headers = {"X-CSRF-Token": csrf}
        with patch("app.modules.scheduler.get_scheduler", return_value=scheduler), patch(
            "app.agent.tools.config.get", side_effect=self._config_get(values)
        ):
            prepared = self.client.post(
                "/api/agent/query",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "message": "立即执行 STRM 同步"},
            )
            self.assertEqual(prepared.status_code, 200, prepared.text)
            body = prepared.json()
            self.assertEqual(body["mode"], "confirmation_required")
            confirmation_id = body["action_plan"]["plan_id"]
            self.assertEqual(body["action_plan"]["plan_id"], confirmation_id)
            self.assertEqual(body["action_plan"]["status"], "awaiting_approval")
            self.assertEqual(
                [item["label"] for item in body["action_plan"]["decisions"]],
                ["执行", "取消"],
            )
            scheduler.trigger.assert_not_called()
            self.assertNotIn("secret-source", prepared.text)
            self.assertNotIn("private-service", prepared.text)
            self.assertNotIn("/private/root", prepared.text)

            direct = self.client.post(
                "/api/agent/tools/strm.run_once", headers=headers, json={"session_id": "test_session_identifier_0001", "arguments": {}}
            )
            self.assertEqual(direct.status_code, 409, direct.text)
            scheduler.trigger.assert_not_called()

            confirmed = self.client.post(
                "/api/agent/actions/confirm",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "plan_id": confirmation_id},
            )
            self.assertEqual(confirmed.status_code, 202, confirmed.text)
            self.assertEqual(confirmed.json()["result"]["status"], "accepted")
            self.assertEqual(confirmed.json()["action_plan"]["status"], "completed")
            self.assertNotIn("private", confirmed.text)
            scheduler.trigger.assert_called_once_with("manual")

            replay = self.client.post(
                "/api/agent/actions/confirm",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "plan_id": confirmation_id},
            )
            self.assertEqual(replay.status_code, 409, replay.text)
            scheduler.trigger.assert_called_once_with("manual")

    def test_query_rejects_natural_language_confirmation_and_preserves_plan(self):
        csrf = self.login()
        scheduler = Mock()
        scheduler.validate_config.return_value = ""
        scheduler.status.return_value = {"running": False}
        scheduler.trigger.return_value = {"ok": True, "message": "done"}
        values = {
            "AGENT_ENABLED": "1",
            "GY_STRM_SOURCE_DIRS": '[{"id":"one","name":"One"}]',
            "GY_STRM_BASE_URL": "http://service",
            "STRM_ROOT": "/root/one",
        }
        headers = {"X-CSRF-Token": csrf}
        session_id = "test_session_identifier_0002"
        with patch("app.modules.scheduler.get_scheduler", return_value=scheduler), patch(
            "app.agent.tools.config.get", side_effect=self._config_get(values)
        ):
            prepared = self.client.post(
                "/api/agent/query",
                headers=headers,
                json={"session_id": session_id, "message": "立即执行 STRM 同步"},
            )
            self.assertEqual(prepared.status_code, 200, prepared.text)
            plan_id = prepared.json()["action_plan"]["plan_id"]

            rejected = self.client.post(
                "/api/agent/query",
                headers=headers,
                json={"session_id": session_id, "message": "好的帮我执行", "stream": True},
            )

            self.assertEqual(rejected.status_code, 409, rejected.text)
            self.assertIn("行动计划卡片", rejected.text)
            scheduler.trigger.assert_not_called()

            confirmed = self.client.post(
                "/api/agent/actions/confirm",
                headers=headers,
                json={"session_id": session_id, "plan_id": plan_id},
            )
            self.assertEqual(confirmed.status_code, 202, confirmed.text)
            scheduler.trigger.assert_called_once_with("manual")

    def test_query_rejects_natural_language_cancellation_and_preserves_plan(self):
        csrf = self.login()
        scheduler = Mock()
        scheduler.validate_config.return_value = ""
        scheduler.status.return_value = {"running": False}
        values = {
            "AGENT_ENABLED": "1",
            "GY_STRM_SOURCE_DIRS": '[{"id":"one","name":"One"}]',
            "GY_STRM_BASE_URL": "http://service",
            "STRM_ROOT": "/root/one",
        }
        headers = {"X-CSRF-Token": csrf}
        session_id = "test_session_identifier_0003"
        with patch("app.modules.scheduler.get_scheduler", return_value=scheduler), patch(
            "app.agent.tools.config.get", side_effect=self._config_get(values)
        ):
            prepared = self.client.post(
                "/api/agent/query",
                headers=headers,
                json={"session_id": session_id, "message": "立即执行 STRM 同步"},
            )
            plan_id = prepared.json()["action_plan"]["plan_id"]

            rejected = self.client.post(
                "/api/agent/query",
                headers=headers,
                json={"session_id": session_id, "message": "取消"},
            )

            self.assertEqual(rejected.status_code, 409, rejected.text)
            self.assertIn("行动计划卡片", rejected.text)
            scheduler.trigger.assert_not_called()

            discarded = self.client.post(
                "/api/agent/actions/confirm/discard",
                headers=headers,
                json={"session_id": session_id, "plan_id": plan_id},
            )
            self.assertEqual(discarded.status_code, 200, discarded.text)
            self.assertTrue(discarded.json()["discarded"])
            stale = self.client.post(
                "/api/agent/actions/confirm",
                headers=headers,
                json={"session_id": session_id, "plan_id": plan_id},
            )
            self.assertEqual(stale.status_code, 409, stale.text)

    def test_explicit_prepare_rejects_arguments_and_stale_configuration(self):
        csrf = self.login()
        scheduler = Mock()
        scheduler.validate_config.return_value = ""
        scheduler.status.return_value = {"running": False}
        scheduler.trigger.return_value = {"ok": True}
        values = {
            "AGENT_ENABLED": "1",
            "GY_STRM_SOURCE_DIRS": '[{"id":"one","name":"One"}]',
            "GY_STRM_BASE_URL": "http://service",
            "STRM_ROOT": "/root/one",
        }
        headers = {"X-CSRF-Token": csrf}
        with patch("app.modules.scheduler.get_scheduler", return_value=scheduler), patch(
            "app.agent.tools.config.get", side_effect=self._config_get(values)
        ):
            rejected = self.client.post(
                "/api/agent/actions/strm.run_once/prepare",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "arguments": {"trigger": "cron"}},
            )
            self.assertEqual(rejected.status_code, 400, rejected.text)

            prepared = self.client.post(
                "/api/agent/actions/strm.run_once/prepare",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "arguments": {}},
            )
            self.assertEqual(prepared.status_code, 200, prepared.text)
            confirmation_id = prepared.json()["action_plan"]["plan_id"]
            values["STRM_ROOT"] = "/root/two"
            stale = self.client.post(
                "/api/agent/actions/confirm",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "plan_id": confirmation_id},
            )
            self.assertEqual(stale.status_code, 409, stale.text)
            self.assertIn("配置已变化", stale.json()["error"])
            scheduler.trigger.assert_not_called()

    def test_confirm_requires_csrf_and_strict_shape(self):
        csrf = self.login()
        missing_csrf = self.client.post(
            "/api/agent/actions/confirm", json={"session_id": "test_session_identifier_0001", "plan_id": "x" * 24}
        )
        self.assertEqual(missing_csrf.status_code, 403)
        headers = {"X-CSRF-Token": csrf}
        for payload in (
            {},
            {"plan_id": 1},
            {"plan_id": "short"},
            {"plan_id": "x" * 24, "arguments": {}},
            {"confirmation_id": 1},
            {"confirmation_id": "short"},
            {"confirmation_id": "x" * 24, "arguments": {}},
            {"plan_id": "x" * 24, "confirmation_id": "y" * 24},
        ):
            with self.subTest(payload=payload):
                response = self.client.post("/api/agent/actions/confirm", headers=headers, json=payload)
                self.assertEqual(response.status_code, 400, response.text)

    def test_runtime_disable_before_confirm_keeps_plan_retryable(self):
        csrf = self.login()
        headers = {"X-CSRF-Token": csrf}
        session_id = "runtime-retry-1234567890abcdef"
        scheduler = Mock()
        scheduler.validate_config.return_value = ""
        scheduler.status.return_value = {"running": False}
        scheduler.trigger.return_value = {"ok": True}
        values = {
            "AGENT_ENABLED": "1",
            "GY_STRM_SOURCE_DIRS": '[{"id":"one","name":"One"}]',
            "GY_STRM_BASE_URL": "http://service",
            "STRM_ROOT": "/root/one",
        }
        with patch("app.modules.scheduler.get_scheduler", return_value=scheduler), patch(
            "app.agent.tools.config.get", side_effect=self._config_get(values)
        ), patch("app.agent.feature_gate.is_agent_enabled", return_value=True):
            prepared = self.client.post(
                "/api/agent/actions/strm.run_once/prepare",
                headers=headers,
                json={"arguments": {}, "session_id": session_id},
            )
            self.assertEqual(prepared.status_code, 200, prepared.text)
            plan_id = prepared.json()["action_plan"]["plan_id"]

            with patch(
                "app.routes.agent_api.agent_runtime_admission",
                side_effect=AgentRuntimeDisabled("Media Agent 已关闭"),
            ):
                blocked = self.client.post(
                    "/api/agent/actions/confirm",
                    headers=headers,
                    json={"plan_id": plan_id, "session_id": session_id},
                )

            self.assertEqual(blocked.status_code, 409, blocked.text)
            self.assertEqual(blocked.json()["code"], "agent_runtime_disabled")
            self.assertTrue(blocked.json()["retryable"])
            scheduler.trigger.assert_not_called()

            confirmed = self.client.post(
                "/api/agent/actions/confirm",
                headers=headers,
                json={"plan_id": plan_id, "session_id": session_id},
            )
            self.assertEqual(confirmed.status_code, 202, confirmed.text)
            scheduler.trigger.assert_called_once_with("manual")

    def test_runtime_change_during_direct_prepare_revokes_unpublished_plan(self):
        csrf = self.login()
        headers = {"X-CSRF-Token": csrf}
        session_id = "prepare-runtime-race-123456789"
        scheduler = Mock()
        scheduler.validate_config.return_value = ""
        scheduler.status.return_value = {"running": False}
        scheduler.trigger.return_value = {"ok": True}
        values = {
            "AGENT_ENABLED": "1",
            "GY_STRM_SOURCE_DIRS": '[{"id":"one","name":"One"}]',
            "GY_STRM_BASE_URL": "http://service",
            "STRM_ROOT": "/root/one",
        }
        service = get_agent_service()
        original_prepare = service.prepare
        captured: dict[str, str] = {}

        def capture_prepare(*args, **kwargs):
            response = original_prepare(*args, **kwargs)
            captured["plan_id"] = response["action_plan"]["plan_id"]
            return response

        with patch("app.modules.scheduler.get_scheduler", return_value=scheduler), patch(
            "app.agent.tools.config.get", side_effect=self._config_get(values)
        ), patch("app.agent.feature_gate.is_agent_enabled", return_value=True), patch.object(
            service, "prepare", side_effect=capture_prepare
        ), patch(
            "app.routes.agent_api.agent_runtime_admission",
            side_effect=AgentRuntimeDisabled("Media Agent 状态已变化"),
        ):
            blocked = self.client.post(
                "/api/agent/actions/strm.run_once/prepare",
                headers=headers,
                json={"arguments": {}, "session_id": session_id},
            )

        self.assertEqual(blocked.status_code, 409, blocked.text)
        self.assertEqual(blocked.json()["code"], "agent_runtime_disabled")
        self.assertTrue(blocked.json()["retryable"])
        self.assertIn("plan_id", captured)

        with patch("app.modules.scheduler.get_scheduler", return_value=scheduler), patch(
            "app.agent.feature_gate.is_agent_enabled", return_value=True
        ):
            stale = self.client.post(
                "/api/agent/actions/confirm",
                headers=headers,
                json={"plan_id": captured["plan_id"], "session_id": session_id},
            )
        self.assertEqual(stale.status_code, 409, stale.text)
        scheduler.trigger.assert_not_called()

    def test_runtime_change_before_query_publication_revokes_generated_plan(self):
        csrf = self.login()
        headers = {"X-CSRF-Token": csrf}
        session_id = "query-runtime-race-12345678901"
        scheduler = Mock()
        scheduler.validate_config.return_value = ""
        scheduler.status.return_value = {"running": False}
        scheduler.trigger.return_value = {"ok": True}
        values = {
            "AGENT_ENABLED": "1",
            "GY_STRM_SOURCE_DIRS": '[{"id":"one","name":"One"}]',
            "GY_STRM_BASE_URL": "http://service",
            "STRM_ROOT": "/root/one",
        }
        service = get_agent_service()
        original_query = service.query
        captured: dict[str, str] = {}

        def capture_query(*args, **kwargs):
            response = original_query(*args, **kwargs)
            captured["plan_id"] = response["action_plan"]["plan_id"]
            return response

        with patch("app.modules.scheduler.get_scheduler", return_value=scheduler), patch(
            "app.agent.tools.config.get", side_effect=self._config_get(values)
        ), patch("app.agent.feature_gate.is_agent_enabled", return_value=True), patch.object(
            service, "query", side_effect=capture_query
        ), patch(
            "app.routes.agent_api.agent_runtime_admission",
            side_effect=AgentRuntimeDisabled("Media Agent 状态已变化"),
        ):
            blocked = self.client.post(
                "/api/agent/query",
                headers=headers,
                json={"message": "立即执行 STRM 同步", "session_id": session_id},
            )

        self.assertEqual(blocked.status_code, 409, blocked.text)
        self.assertEqual(blocked.json()["code"], "agent_runtime_disabled")
        self.assertIn("plan_id", captured)

        with patch("app.modules.scheduler.get_scheduler", return_value=scheduler), patch(
            "app.agent.feature_gate.is_agent_enabled", return_value=True
        ):
            stale = self.client.post(
                "/api/agent/actions/confirm",
                headers=headers,
                json={"plan_id": captured["plan_id"], "session_id": session_id},
            )
        self.assertEqual(stale.status_code, 409, stale.text)
        scheduler.trigger.assert_not_called()

    def test_web_sessions_isolate_reset_and_confirmation_tickets(self):
        csrf = self.login()
        headers = {"X-CSRF-Token": csrf}
        session_a = "session-a-1234567890abcdef"
        session_b = "session-b-1234567890abcdef"
        scheduler = Mock()
        scheduler.validate_config.return_value = ""
        scheduler.status.return_value = {"running": False}
        scheduler.trigger.return_value = {"ok": True}
        values = {
            "AGENT_ENABLED": "1",
            "GY_STRM_SOURCE_DIRS": '[{"id":"one","name":"One"}]',
            "GY_STRM_BASE_URL": "http://service",
            "STRM_ROOT": "/root/one",
        }
        with patch("app.modules.scheduler.get_scheduler", return_value=scheduler), patch(
            "app.agent.tools.config.get", side_effect=self._config_get(values)
        ):
            prepared_a = self.client.post(
                "/api/agent/actions/strm.run_once/prepare",
                headers=headers,
                json={"arguments": {}, "session_id": session_a},
            )
            self.assertEqual(prepared_a.status_code, 200, prepared_a.text)
            ticket_a = prepared_a.json()["action_plan"]["plan_id"]

            wrong_session = self.client.post(
                "/api/agent/actions/confirm",
                headers=headers,
                json={"plan_id": ticket_a, "session_id": session_b},
            )
            self.assertEqual(wrong_session.status_code, 409, wrong_session.text)

            reset = self.client.post(
                "/api/agent/session/reset",
                headers=headers,
                json={"session_id": session_a},
            )
            self.assertEqual(reset.status_code, 200, reset.text)
            self.assertTrue(reset.json()["reset"])
            stale = self.client.post(
                "/api/agent/actions/confirm",
                headers=headers,
                json={"plan_id": ticket_a, "session_id": session_a},
            )
            self.assertEqual(stale.status_code, 409, stale.text)

            prepared_b = self.client.post(
                "/api/agent/actions/strm.run_once/prepare",
                headers=headers,
                json={"arguments": {}, "session_id": session_b},
            )
            ticket_b = prepared_b.json()["action_plan"]["plan_id"]
            self.client.post(
                "/api/agent/session/reset",
                headers=headers,
                json={"session_id": session_a},
            )
            confirmed_b = self.client.post(
                "/api/agent/actions/confirm",
                headers=headers,
                json={"plan_id": ticket_b, "session_id": session_b},
            )
            self.assertEqual(confirmed_b.status_code, 202, confirmed_b.text)
            scheduler.trigger.assert_called_once_with("manual")

    def test_session_reset_requires_csrf_and_strict_session_id(self):
        csrf = self.login()
        self.assertEqual(
            self.client.post(
                "/api/agent/session/reset",
                json={"session_id": "session-a-1234567890abcdef"},
            ).status_code,
            403,
        )
        headers = {"X-CSRF-Token": csrf}
        for payload in (
            {},
            {"session_id": "short"},
            {"session_id": "session-a-1234567890abcdef", "extra": True},
        ):
            with self.subTest(payload=payload):
                response = self.client.post(
                    "/api/agent/session/reset",
                    headers=headers,
                    json=payload,
                )
                self.assertEqual(response.status_code, 400, response.text)


if __name__ == "__main__":
    unittest.main()
