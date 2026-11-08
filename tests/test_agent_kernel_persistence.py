from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app import database as db
from app.agent.kernel.events import AgentEvent, AgentEventType
from app.agent.kernel.persistence import (
    _DUE_EFFECT_WAITS_SQL,
    SQLiteKernelStore,
)
from app.agent.kernel.references import InMemoryReferenceStore, ReferenceError
from app.agent.kernel.session_guard import session_scope_guard
from app.agent.kernel.state import InMemorySessionStateStore
from app.agent.kernel.state_types import StalePublicationError, StateUpdate, _PublicationFinished


class SQLiteKernelStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.previous_path = db.DB_PATH
        self.previous_test_mode = bool(getattr(db, "_configured_test_mode", False))
        db.configure_database(Path(self.temp.name) / "kernel.db", test_mode=True)
        db.init_db()
        self.store = SQLiteKernelStore(secret_provider=lambda: "test-kernel-secret")

    async def asyncTearDown(self) -> None:
        db.configure_database(self.previous_path, test_mode=self.previous_test_mode)
        self.temp.cleanup()

    async def test_session_generation_persists_and_rejects_late_commit(self) -> None:
        first, _ = await self.store.begin_turn(
            owner="owner", session_id="session", request_id="one"
        )
        await self.store.commit(
            first,
            conversation=[{"role": "user", "content": "hello"}],
            updates=(StateUpdate("summary", "saved"),),
        )
        reloaded = SQLiteKernelStore(secret_provider=lambda: "test-kernel-secret")
        state = await reloaded.load(owner="owner", session_id="session")
        self.assertEqual(state.summary, "saved")
        self.assertEqual(state.conversation[-1]["content"], "hello")

        second, _ = await reloaded.begin_turn(
            owner="owner", session_id="session", request_id="two"
        )
        self.assertEqual(second.generation, first.generation + 1)
        with self.assertRaises(StalePublicationError):
            await self.store.commit(first, updates=(StateUpdate("summary", "late"),))

    async def test_effect_rmw_uses_latest_state_and_preserves_publication_fields(self) -> None:
        first, _ = await self.store.begin_turn(
            owner="owner", session_id="session", request_id="first",
        )
        await self.store.commit(
            first,
            conversation=[{"role": "user", "content": "最新用户消息"}],
            updates=(StateUpdate("pending_effect_plan_id", "pending-plan"),),
        )
        second, _ = await self.store.begin_turn(
            owner="owner", session_id="session", request_id="second",
        )
        observed = []

        def change(state):
            observed.append((state.generation, state.pending_effect_plan_id,
                             state.conversation[-1]["content"]))
            state.generation += 100
            state.pending_effect_plan_id = "wrong-plan"
            state.metadata["effect_marker"] = "claimed"
            return "updated"

        self.assertEqual(
            await self.store.update_effect_state(
                owner="owner", session_id="session", change=change,
            ),
            "updated",
        )
        latest = await self.store.load(owner="owner", session_id="session")
        self.assertEqual(observed, [(second.generation, "pending-plan", "最新用户消息")])
        self.assertEqual(latest.generation, second.generation)
        self.assertEqual(latest.pending_effect_plan_id, "pending-plan")
        self.assertEqual(latest.metadata["effect_marker"], "claimed")
        self.assertTrue(await self.store.is_current(second))

    async def test_effect_rmw_exception_rolls_back_and_noop_skips_write(self) -> None:
        await self.store.begin_turn(owner="owner", session_id="session", request_id="one")
        before = await self.store.load(owner="owner", session_id="session")

        def fail(state):
            state.metadata["partial"] = True
            raise RuntimeError("rollback")

        with self.assertRaisesRegex(RuntimeError, "rollback"):
            await self.store.update_effect_state(
                owner="owner", session_id="session", change=fail,
            )
        self.assertEqual(await self.store.load(owner="owner", session_id="session"), before)

        original_write = self.store._write_state
        writes = []

        def track_write(conn, state):
            writes.append(state.clone())
            return original_write(conn, state)

        self.store._write_state = track_write
        try:
            await self.store.update_effect_state(
                owner="owner", session_id="session", change=lambda _state: "unchanged",
            )
        finally:
            self.store._write_state = original_write
        self.assertEqual(writes, [])

    async def test_effect_rmw_reenters_confirmed_effect_guard(self) -> None:
        await self.store.begin_turn(owner="owner", session_id="session", request_id="one")
        with session_scope_guard("owner", "session", kind="effect"):
            result = await self.store.update_effect_state(
                owner="owner",
                session_id="session",
                change=lambda state: state.metadata.update({"effect_guard": "reentered"}),
            )
        self.assertIsNone(result)
        state = await self.store.load(owner="owner", session_id="session")
        self.assertEqual(state.metadata["effect_guard"], "reentered")

    async def test_commit_merges_late_receipt_by_id_without_touching_other_messages(self) -> None:
        lease, _ = await self.store.begin_turn(
            owner="owner", session_id="session", request_id="one",
        )
        await self.store.update_effect_state(
            owner="owner",
            session_id="session",
            change=lambda state: state.metadata.update({
                "effect_waits": {
                    "plan-a": {"receipt_message": {
                        "role": "assistant", "content": "completed", "completion_receipt_id": "plan-a",
                    }},
                    "plan-b": {"receipt_message": {
                        "role": "assistant", "content": "mismatched", "completion_receipt_id": "other-id",
                    }},
                },
            }),
        )
        committed = await self.store.commit(
            lease,
            conversation=[
                {"role": "assistant", "content": "stale placeholder", "completion_receipt_id": "plan-a"},
                {"role": "assistant", "content": "duplicate", "completion_receipt_id": "plan-a"},
                {"role": "assistant", "content": "unrelated", "completion_receipt_id": "unrelated-id"},
            ],
        )
        self.assertEqual(
            [message.get("completion_receipt_id") for message in committed.conversation],
            ["plan-a", "unrelated-id"],
        )
        self.assertEqual(committed.conversation[0]["content"], "completed")
        self.assertEqual(committed.conversation[1]["content"], "unrelated")

    async def test_begin_turn_consumes_delivered_receipt_without_permanent_resurrection(self) -> None:
        first, _ = await self.store.begin_turn(
            owner="owner", session_id="session", request_id="first",
        )
        await self.store.commit(
            first,
            conversation=[{"role": "user", "content": str(index)} for index in range(80)],
        )

        def add_waits(state):
            state.metadata["effect_waits"] = {
                "delivered": {
                    "delivered": True,
                    "next_poll_at": 3,
                    "receipt_message": {
                        "role": "assistant", "content": "final", "completion_receipt_id": "delivered",
                    },
                },
                "awaiting-delivery": {"delivered": False, "next_poll_at": 20},
            }
            state.metadata["effect_next_poll_at"] = 3

        await self.store.update_effect_state(
            owner="owner", session_id="session", change=add_waits,
        )
        second, state = await self.store.begin_turn(
            owner="owner", session_id="session", request_id="second",
        )
        self.assertEqual(len(state.conversation), 81)
        self.assertEqual(state.conversation[-1]["completion_receipt_id"], "delivered")
        self.assertEqual(list(state.metadata["effect_waits"]), ["awaiting-delivery"])
        self.assertEqual(state.metadata["effect_next_poll_at"], 20)

        await self.store.commit(
            second,
            conversation=[{"role": "user", "content": f"new-{index}"} for index in range(80)],
        )
        after = await self.store.load(owner="owner", session_id="session")
        self.assertFalse(any(
            item.get("completion_receipt_id") == "delivered" for item in after.conversation
        ))
        self.assertEqual(list(after.metadata["effect_waits"]), ["awaiting-delivery"])

    async def test_due_waits_preserve_sealed_bytes_skip_bad_hmac_and_do_not_recreate_deleted(self) -> None:
        now = [100.0]
        store = SQLiteKernelStore(
            secret_provider=lambda: "test-kernel-secret",
            clock=lambda: now[0],
        )
        for session_id in ("valid", "tampered"):
            await store.begin_turn(owner="owner", session_id=session_id, request_id="one")

            def add_due(state):
                state.metadata["effect_waits"] = {
                    "due-plan": {"next_poll_at": 99, "sealed": "enc:v1:not-decoded"},
                    "future-plan": {"next_poll_at": 101, "sealed": "future"},
                }
                state.metadata["effect_next_poll_at"] = 99

            await store.update_effect_state(
                owner="owner", session_id=session_id, change=add_due,
            )

        owner_digest, session_digest = store._scope("owner", "tampered")
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE agent_kernel_sessions SET state_hmac='broken' "
                "WHERE owner_digest=? AND session_digest=?",
                (owner_digest, session_digest),
            )
        self.assertEqual(
            await store.due_effect_waits(),
            [{"next_poll_at": 99, "sealed": "enc:v1:not-decoded", "plan_id": "due-plan"}],
        )

        await store.reset_session(owner="owner", session_id="valid")
        await store.begin_turn(owner="owner", session_id="valid", request_id="after-reset")
        self.assertEqual(await store.due_effect_waits(), [])
        await store.update_effect_state(
            owner="owner", session_id="valid",
            change=lambda state: state.metadata.update({
                "effect_waits": {"doomed": {"next_poll_at": 99, "sealed": "private"}},
                "effect_next_poll_at": 99,
            }),
        )
        self.assertTrue(await store.delete_session(owner="owner", session_id="valid"))
        self.assertIsNone(await store.update_effect_state(
            owner="owner", session_id="valid", change=lambda state: state.metadata.clear(),
        ))
        with db.get_conn() as conn:
            owner_digest, session_digest = store._scope("owner", "valid")
            row = conn.execute(
                "SELECT 1 FROM agent_kernel_sessions WHERE owner_digest=? AND session_digest=?",
                (owner_digest, session_digest),
            ).fetchone()
        self.assertIsNone(row)
        self.assertEqual(await store.due_effect_waits(), [])

    async def test_delivered_receipts_do_not_fill_poll_limit_or_lose_continuations(self) -> None:
        for store in (InMemorySessionStateStore(clock=lambda: 100),
                      SQLiteKernelStore(secret_provider=lambda: "test-kernel-secret", clock=lambda: 100)):
            with self.subTest(store=type(store).__name__):
                await store.begin_turn(owner="owner", session_id="session", request_id="one")

                def add_waits(state):
                    state.metadata["effect_waits"] = {
                        **{f"delivered-{index:02}": {
                            "next_poll_at": index + 1, "delivered": True,
                            "continuation": {"state": "pending", "wait_id": f"wait-{index}"},
                            "receipt_message": {"content": f"receipt-{index}"},
                        } for index in range(16)},
                        "unfinished": {"next_poll_at": 99, "delivered": False, "sealed": "pending"},
                        "future": {"next_poll_at": 101, "delivered": False, "sealed": "future"},
                    }
                    state.metadata["effect_next_poll_at"] = 99

                await store.update_effect_state(owner="owner", session_id="session", change=add_waits)
                before = await store.load(owner="owner", session_id="session")
                for limit in (1, 16):
                    self.assertEqual(
                        [record["plan_id"] for record in await store.due_effect_waits(limit=limit)],
                        ["unfinished"],
                    )
                self.assertEqual(await store.load(owner="owner", session_id="session"), before)

    async def test_due_query_uses_expression_index_and_limits_candidate_sessions(self) -> None:
        now = [100.0]
        store = SQLiteKernelStore(
            secret_provider=lambda: "test-kernel-secret",
            clock=lambda: now[0],
        )
        for session_id, poll_at, plans in (
            ("earliest", 90, ("plan-a", "plan-b")),
            ("later", 95, ("plan-c",)),
        ):
            await store.begin_turn(owner="owner", session_id=session_id, request_id="one")

            def add_waits(state, *, poll_at=poll_at, plans=plans):
                state.metadata["effect_waits"] = {
                    plan_id: {"next_poll_at": poll_at, "sealed": f"sealed:{plan_id}"}
                    for plan_id in plans
                }
                state.metadata["effect_next_poll_at"] = poll_at

            await store.update_effect_state(
                owner="owner", session_id=session_id, change=add_waits,
            )

        with db.get_conn() as conn:
            plan = conn.execute(
                "EXPLAIN QUERY PLAN " + _DUE_EFFECT_WAITS_SQL,
                (now[0], 1),
            ).fetchall()
        details = " ".join(str(row["detail"]) for row in plan)
        self.assertIn("idx_agent_kernel_effect_next_poll_at", details)
        self.assertNotIn("USE TEMP B-TREE FOR ORDER BY", details)

        with patch.object(store, "_decode", wraps=store._decode) as decode:
            due = await store.due_effect_waits(limit=1)
        self.assertEqual(decode.call_count, 1)
        self.assertEqual([record["plan_id"] for record in due], ["plan-a"])
        self.assertEqual(
            [record["plan_id"] for record in await store.due_effect_waits(limit=16)],
            ["plan-a", "plan-b", "plan-c"],
        )

    async def test_reference_deadline_uses_remaining_ttl_and_same_scope_checks(self) -> None:
        for store_type in (InMemoryReferenceStore, SQLiteKernelStore):
            with self.subTest(store=store_type.__name__):
                now = [2_000_000_000.0]
                store = store_type(clock=lambda now=now: now[0])
                reference = await store.put(owner="owner", session_id="session", kind="payload", value={"id": 1})
                now[0] += 850
                deadline = await store.deadline(reference.ref, owner="owner", session_id="session", expected_kind="payload")
                self.assertAlmostEqual(deadline - time.monotonic(), 50, delta=0.02)
                for owner, session, kind in (("other", "session", "payload"), ("owner", "other", "payload"),
                                             ("owner", "session", "wrong")):
                    with self.assertRaises(ReferenceError):
                        await store.deadline(reference.ref, owner=owner, session_id=session, expected_kind=kind)
                now[0] += 51
                with self.assertRaises(ReferenceError):
                    await store.deadline(reference.ref, owner="owner", session_id="session", expected_kind="payload")

    async def test_reference_deadline_is_read_only_and_checks_persisted_integrity(self) -> None:
        reference = await self.store.put(owner="owner", session_id="session", kind="payload", value={"id": 1})
        with db.get_conn() as conn:
            original = dict(conn.execute("SELECT * FROM agent_kernel_refs WHERE ref_id=?", (reference.ref,)).fetchone())
        await self.store.deadline(reference.ref, owner="owner", session_id="session", expected_kind="payload")
        with db.get_conn() as conn:
            observed = dict(conn.execute("SELECT * FROM agent_kernel_refs WHERE ref_id=?", (reference.ref,)).fetchone())
            self.assertEqual(observed, original)
            conn.execute("UPDATE agent_kernel_refs SET expires_at=expires_at+60 WHERE ref_id=?", (reference.ref,))
        with self.assertRaisesRegex(ReferenceError, "payload is invalid"):
            await self.store.deadline(reference.ref, owner="owner", session_id="session", expected_kind="payload")

    async def test_reference_is_opaque_owner_scoped_and_persistent(self) -> None:
        reference = await self.store.put(
            owner="owner",
            session_id="session",
            kind="cloud_directory",
            value={"id": 1938, "path": "/private/path"},
        )
        self.assertTrue(reference.ref.startswith("ref_"))
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT value_json FROM agent_kernel_refs WHERE ref_id=?",
                (reference.ref,),
            ).fetchone()
        self.assertTrue(str(row["value_json"]).startswith("enc:v1:"))
        self.assertNotIn("/private/path", str(row["value_json"]))
        resolved = await SQLiteKernelStore(
            secret_provider=lambda: "test-kernel-secret"
        ).resolve(
            reference.ref,
            owner="owner",
            session_id="session",
            expected_kind="cloud_directory",
        )
        self.assertEqual(resolved["id"], 1938)
        with self.assertRaises(ReferenceError):
            await self.store.resolve(
                reference.ref,
                owner="other",
                session_id="session",
                expected_kind="cloud_directory",
            )

    async def test_event_retention_caps_session_and_removes_expired_rows(self) -> None:
        now = [10_000.0]
        store = SQLiteKernelStore(
            secret_provider=lambda: "test-kernel-secret",
            clock=lambda: now[0],
            max_events_per_session=10,
            event_retention_seconds=3_600,
        )
        for sequence in range(1, 13):
            await store.append(
                AgentEvent(
                    type=AgentEventType.MODEL_DELTA,
                    session_id="retained-session",
                    turn_id="turn-a",
                    request_id="request-a",
                    sequence=sequence,
                    payload={"delta": str(sequence)},
                ),
                owner="owner",
            )
        capped = await store.list_events(
            owner="owner", session_id="retained-session"
        )
        self.assertEqual([item["sequence"] for item in capped], list(range(3, 13)))

        now[0] += 3_601
        await store.append(
            AgentEvent(
                type=AgentEventType.TURN_COMPLETED,
                session_id="retained-session",
                turn_id="turn-b",
                request_id="request-b",
                sequence=1,
                payload={"status": "success"},
            ),
            owner="owner",
        )
        remaining = await store.list_events(
            owner="owner", session_id="retained-session"
        )
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["turn_id"], "turn-b")

    async def test_active_journal_survives_count_current_writer_expiry_and_cold_store(self) -> None:
        now = [10_000.0]
        store = SQLiteKernelStore(secret_provider=lambda: "test-kernel-secret", clock=lambda: now[0],
                                  max_events_per_session=10, event_retention_seconds=3_600)
        lease, _ = await store.begin_turn(owner="owner", session_id="session", request_id="request")
        await store.append_for_turn(AgentEvent(AgentEventType.TURN_STARTED, lease.session_id, lease.turn_id,
            lease.request_id, 1, {"stream_display_enabled": True}), lease)
        for sequence in range(2, 28):
            await store.append_for_turn(AgentEvent(AgentEventType.MODEL_DELTA, lease.session_id, lease.turn_id,
                lease.request_id, sequence, {"delta": str(sequence)}), lease)
        now[0] += 3_601
        store._events_since_global_prune = 127
        await store.append_for_turn(AgentEvent(AgentEventType.MODEL_DELTA, lease.session_id, lease.turn_id,
            lease.request_id, 28, {"delta": "still active"}), lease)
        rebuilt = SQLiteKernelStore(secret_provider=lambda: "test-kernel-secret", clock=lambda: now[0],
                                    max_events_per_session=10, event_retention_seconds=3_600)
        events = await rebuilt.list_events(owner="owner", session_id="session", limit=1,
                                          request_id=lease.request_id, turn_id=lease.turn_id)
        self.assertEqual([event["sequence"] for event in events], list(range(1, 29)))
        self.assertTrue(events[0]["payload"]["stream_display_enabled"])
        self.assertEqual(len(await rebuilt.list_events(owner="owner", session_id="session", limit=3)), 3)
        for sequence in range(29, 44):
            await rebuilt.append_for_turn(AgentEvent(AgentEventType.MODEL_DELTA, lease.session_id, lease.turn_id,
                lease.request_id, sequence, {"delta": "fresh"}), lease)
        successor, _ = await rebuilt.begin_turn(owner="owner", session_id="session", request_id="successor")
        await rebuilt.append_for_turn(AgentEvent(AgentEventType.TURN_STARTED, successor.session_id, successor.turn_id,
            successor.request_id, 1), successor)
        history = await rebuilt.list_events(owner="owner", session_id="session", request_id=lease.request_id,
                                            turn_id=lease.turn_id)
        self.assertEqual([event["sequence"] for event in history], list(range(34, 44)))
        with self.assertRaises(StalePublicationError):
            await rebuilt.append_for_turn(AgentEvent(AgentEventType.MODEL_DELTA, lease.session_id, lease.turn_id,
                lease.request_id, 44, {"delta": "late"}), lease)

    async def test_failure_without_lease_is_audited_when_session_state_signature_is_invalid(self) -> None:
        lease, _ = await self.store.begin_turn(owner="owner", session_id="session", request_id="original")
        owner_digest, session_digest = self.store._scope("owner", "session")
        with db.get_conn() as conn:
            conn.execute("UPDATE agent_kernel_sessions SET state_json='{}' WHERE owner_digest=? AND session_digest=?",
                         (owner_digest, session_digest))
        failure = AgentEvent(AgentEventType.TURN_FAILED, "session", "admission-failure", "rejected", 1,
                             {"code": "admission_failed"})
        await self.store.append(failure, owner="owner")
        self.assertEqual([event["event_id"] for event in await self.store.list_events(owner="owner", session_id="session")],
                         [failure.event_id])
        with self.assertRaises(ValueError):
            await self.store.append_for_turn(AgentEvent(AgentEventType.MODEL_DELTA, lease.session_id, lease.turn_id,
                lease.request_id, 1, {"delta": "must reject"}), lease)

    async def test_terminal_journal_restores_count_and_expiry_retention(self) -> None:
        now = [10_000.0]
        store = SQLiteKernelStore(secret_provider=lambda: "test-kernel-secret", clock=lambda: now[0],
                                  max_events_per_session=10, event_retention_seconds=3_600)
        for kind in (AgentEventType.TURN_COMPLETED, AgentEventType.TURN_FAILED, AgentEventType.TURN_CANCELLED):
            with self.subTest(terminal=kind):
                sid = "session-" + kind.value
                lease, _ = await store.begin_turn(owner="owner", session_id=sid, request_id="request")
                for sequence in range(1, 19):
                    await store.append_for_turn(AgentEvent(AgentEventType.MODEL_DELTA, sid, lease.turn_id,
                        lease.request_id, sequence, {"delta": str(sequence)}), lease)
                self.assertEqual(len(await store.list_events(owner="owner", session_id=sid,
                    request_id=lease.request_id, turn_id=lease.turn_id)), 18)
                if kind is AgentEventType.TURN_CANCELLED:
                    self.assertTrue(await store.cancel_publication(owner="owner", session_id=sid, request_id=lease.request_id))
                await store.append_for_turn(AgentEvent(kind, sid, lease.turn_id, lease.request_id, 19), lease)
                events = await store.list_events(owner="owner", session_id=sid,
                                                request_id=lease.request_id, turn_id=lease.turn_id)
                self.assertEqual([event["sequence"] for event in events], list(range(10, 20)))
                if kind is not AgentEventType.TURN_CANCELLED:
                    await store.commit(lease, updates=(_PublicationFinished("", None),))
                await store.append(AgentEvent(AgentEventType.TOOL_COMPLETED, sid, "history-turn", "history-request", 1), owner="owner")
                self.assertEqual(len(await store.list_events(owner="owner", session_id=sid)), 10)
                now[0] += 3_601
                store._events_since_global_prune = 127
                await store.append(AgentEvent(AgentEventType.TOOL_COMPLETED, "trigger", "trigger-turn-" + kind.value,
                    "trigger-request", 1), owner="third")
                self.assertEqual(await store.list_events(owner="owner", session_id=sid), [])

    async def test_directed_journal_read_keeps_history_window_and_validates_signed_identity(self) -> None:
        lease, _ = await self.store.begin_turn(owner="owner", session_id="session", request_id="request")
        events = [AgentEvent(AgentEventType.MODEL_DELTA, lease.session_id, lease.turn_id, lease.request_id,
                            sequence, {"delta": str(sequence)}) for sequence in (3, 1, 2)]
        for event in events:
            await self.store.append_for_turn(event, lease)
        directed = await self.store.list_events(owner="owner", session_id="session", limit=1,
            request_id=lease.request_id, turn_id=lease.turn_id)
        self.assertEqual([event["sequence"] for event in directed], [1, 2, 3])
        self.assertEqual(len(await self.store.list_events(owner="owner", session_id="session", limit=1)), 1)
        for owner, sid, request, turn in (("other", "session", lease.request_id, lease.turn_id),
                ("owner", "other", lease.request_id, lease.turn_id),
                ("owner", "session", "other", lease.turn_id), ("owner", "session", lease.request_id, "other")):
            self.assertEqual(await self.store.list_events(owner=owner, session_id=sid, request_id=request, turn_id=turn), [])
        for request, turn in ((lease.request_id, ""), ("", lease.turn_id)):
            with self.assertRaises(ValueError):
                await self.store.list_events(owner="owner", session_id="session", request_id=request, turn_id=turn)
        forged = AgentEvent(AgentEventType.MODEL_DELTA, "session", "other-turn", "other-request", 4, {"delta": "private"})
        await self.store.append(forged, owner="owner")
        with db.get_conn() as conn:
            conn.execute("UPDATE agent_kernel_events SET event_json='{}' WHERE event_id=?", (events[2].event_id,))
            conn.execute("UPDATE agent_kernel_events SET turn_id=?,request_id=? WHERE event_id=?",
                         (lease.turn_id, lease.request_id, forged.event_id))
        restored = await self.store.list_events(owner="owner", session_id="session",
            request_id=lease.request_id, turn_id=lease.turn_id)
        self.assertEqual([event["sequence"] for event in restored], [1, 3])

    async def test_session_listing_reset_and_delete_are_owner_scoped(self) -> None:
        lease, _ = await self.store.begin_turn(
            owner="owner", session_id="session-a", request_id="one"
        )
        await self.store.commit(
            lease,
            conversation=[
                {"role": "user", "content": "检查媒体库有没有缺集"},
                {"role": "assistant", "content": "正在检查"},
            ],
            updates=(StateUpdate("pending_effect_plan_id", "plan-x"),),
        )
        self.assertEqual(
            await self.store.list_sessions(owner="other"),
            [],
        )
        sessions = await self.store.list_sessions(owner="owner")
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["session_id"], "session-a")
        self.assertEqual(sessions[0]["title"], "检查媒体库有没有缺集")
        self.assertTrue(sessions[0]["pending_approval"])

        reset = await self.store.reset_session(owner="owner", session_id="session-a")
        self.assertEqual(reset.conversation, [])
        self.assertEqual(reset.pending_effect_plan_id, "")
        self.assertGreater(reset.generation, lease.generation)
        self.assertTrue(
            await self.store.delete_session(owner="owner", session_id="session-a")
        )
        self.assertEqual(await self.store.list_sessions(owner="owner"), [])

        recreated, _ = await self.store.begin_turn(
            owner="owner", session_id="session-a", request_id="recreated"
        )
        self.assertGreater(recreated.generation, reset.generation)

    async def test_event_journal_records_real_public_events(self) -> None:
        event = AgentEvent(
            type=AgentEventType.TOOL_STARTED,
            session_id="session",
            turn_id="turn",
            request_id="request",
            sequence=1,
            payload={"tool": "cloud.list"},
        )
        await self.store.append(event, owner="owner")
        events = await self.store.list_events(owner="owner", session_id="session")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "tool.started")
        self.assertEqual(events[0]["payload"]["tool"], "cloud.list")
        self.assertEqual(
            await self.store.list_events(owner="other", session_id="session"),
            [],
        )
