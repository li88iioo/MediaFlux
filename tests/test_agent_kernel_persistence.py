from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app import database as db
from app.agent.kernel.events import AgentEvent, AgentEventType
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.references import ReferenceError
from app.agent.kernel.state import StalePublicationError, StateUpdate


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
