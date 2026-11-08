from __future__ import annotations
from app.agent.kernel.session import AgentSession
from typing import cast

import unittest
import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.agent.kernel import sandbox

from app.agent.confirmation import ConfirmationStore
from app.agent.kernel.capabilities import ToolEffect
from app.agent.kernel.effects import ConfirmationEffectPlanStore, EffectPlanError
from app.agent.kernel.tool_calls import PreparedEffect
from app.agent.kernel.lifecycle import AgentSessionLifecycle
from app.agent.kernel.session_state import SessionState
from app.agent.kernel.state import TurnCoordinator
from app.agent.kernel.state_types import PublicationLease, SessionBusyError
from app.concurrency import CrossLoopAsyncLock


class _Session:
    def __init__(self) -> None:
        self._start_lock = CrossLoopAsyncLock(poll_interval=0.001)
        self.coordinator = TurnCoordinator()

    async def cancel(self, *, owner: str, session_id: str) -> bool:
        return await self.coordinator.cancel(
            owner=owner,
            session_id=session_id,
            reason="user_cancelled",
        )


class _Store:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.generation = 4

    async def reset_session(self, *, owner: str, session_id: str) -> SessionState:
        self.calls.append(("reset", owner, session_id))
        self.generation += 1
        return SessionState(owner=owner, session_id=session_id, generation=self.generation)

    async def delete_session(self, *, owner: str, session_id: str) -> bool:
        self.calls.append(("delete", owner, session_id))
        return True


class AgentSessionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        paths = SimpleNamespace(data_dir=Path(directory.name), runtime_dir=Path(directory.name) / "runtime")
        context = patch.object(sandbox, "get_runtime_paths", return_value=paths)
        context.start()
        self.addCleanup(context.stop)
        self.workspaces = {}
        for owner, session in (("owner", "session-a"), ("owner", "session-b"), ("other", "session-a")):
            root, _ = sandbox._workspace_location(owner, session)
            for name in ("work", "output"):
                (root / name).mkdir(parents=True)
                (root / name / "facts.json").write_text('{"old":true}')
            self.workspaces[owner, session] = root

    @staticmethod
    def _prepared(label: str) -> PreparedEffect:
        return PreparedEffect(
            preview={"summary": label},
            snapshot_fingerprint=f"snapshot:{label}",
            metadata={"risk": "write"},
        )

    async def test_reset_cancels_turn_and_revokes_only_current_session(self) -> None:
        session = _Session()
        store = _Store()
        effects = ConfirmationEffectPlanStore(ConfirmationStore())
        first = effects.freeze(
            owner="owner",
            session_id="session-a",
            generation=4,
            tool_name="write.test",
            effect=ToolEffect.WRITE,
            arguments={"value": "first"},
            prepared=self._prepared("first"),
        )
        second = effects.freeze(
            owner="owner",
            session_id="session-b",
            generation=4,
            tool_name="write.test",
            effect=ToolEffect.WRITE,
            arguments={"value": "second"},
            prepared=self._prepared("second"),
        )
        lease = PublicationLease("owner", "session-a", 4, "turn-a", "request-a")
        token = await session.coordinator.begin(lease)
        provider_clears: list[tuple[str, str]] = []
        lifecycle = AgentSessionLifecycle(
            session=cast(AgentSession, session),
            store=store,
            effect_store=effects,
            clear_legacy_plans=lambda *, owner, session_id: provider_clears.append(
                (owner, session_id)
            ),
        )

        state = await lifecycle.reset(owner="owner", session_id="session-a")

        self.assertTrue(token.cancelled)
        self.assertEqual(state.generation, 5)
        self.assertEqual(store.calls, [("reset", "owner", "session-a")])
        self.assertEqual(provider_clears, [("owner", "session-a")])
        self.assertFalse(self.workspaces["owner", "session-a"].exists())
        for scope in (("owner", "session-b"), ("other", "session-a")):
            self.assertTrue((self.workspaces[scope] / "work/facts.json").is_file())
        with self.assertRaisesRegex(EffectPlanError, "unavailable"):
            effects.claim(
                owner="owner",
                session_id="session-a",
                generation=4,
                plan_id=first.plan_id,
            )
        claimed = effects.claim(
            owner="owner",
            session_id="session-b",
            generation=4,
            plan_id=second.plan_id,
        )
        self.assertEqual(claimed.arguments, {"value": "second"})

    async def test_delete_uses_the_same_cleanup_boundary(self) -> None:
        session = _Session()
        store = _Store()
        effects = ConfirmationEffectPlanStore(ConfirmationStore())
        provider_clears: list[tuple[str, str]] = []
        lifecycle = AgentSessionLifecycle(
            session=cast(AgentSession, session),
            store=store,
            effect_store=effects,
            clear_legacy_plans=lambda *, owner, session_id: provider_clears.append(
                (owner, session_id)
            ),
        )

        self.assertTrue(await lifecycle.delete(owner="owner", session_id="session-a"))
        self.assertEqual(store.calls, [("delete", "owner", "session-a")])
        self.assertEqual(provider_clears, [("owner", "session-a")])
        self.assertFalse(self.workspaces["owner", "session-a"].exists())

    async def test_cleanup_waits_for_command_exit_and_removes_its_last_output(self):
        root, lock = sandbox._workspace_location("owner", "session-a")
        self.assertTrue(lock.acquire(blocking=False))
        cleanup = asyncio.create_task(asyncio.to_thread(
            sandbox.clear_workspace, owner="owner", session_id="session-a"))
        try:
            await asyncio.sleep(0.05)
            self.assertFalse(cleanup.done())
            (root / "output/last.json").write_text("last result")
        finally:
            lock.release()
        await cleanup
        self.assertFalse(root.exists())

    async def test_protected_effect_blocks_reset_without_mutating_state(self) -> None:
        session = _Session()
        store = _Store()
        effects = ConfirmationEffectPlanStore(ConfirmationStore())
        plan = effects.freeze(
            owner="owner",
            session_id="session-a",
            generation=4,
            tool_name="write.test",
            effect=ToolEffect.WRITE,
            arguments={"value": "keep"},
            prepared=self._prepared("keep"),
        )
        lease = PublicationLease("owner", "session-a", 4, "turn-a", "request-a")
        token = await session.coordinator.begin(lease, protected=True)
        provider_clears: list[tuple[str, str]] = []
        lifecycle = AgentSessionLifecycle(
            session=cast(AgentSession, session),
            store=store,
            effect_store=effects,
            clear_legacy_plans=lambda *, owner, session_id: provider_clears.append(
                (owner, session_id)
            ),
        )

        with self.assertRaises(SessionBusyError):
            await lifecycle.reset(owner="owner", session_id="session-a")

        self.assertEqual(store.calls, [])
        self.assertEqual(provider_clears, [])
        self.assertFalse(token.cancelled)
        await session.coordinator.finish(lease, token)
        claimed = effects.claim(
            owner="owner",
            session_id="session-a",
            generation=4,
            plan_id=plan.plan_id,
        )
        self.assertEqual(claimed.arguments, {"value": "keep"})


if __name__ == "__main__":
    unittest.main()
