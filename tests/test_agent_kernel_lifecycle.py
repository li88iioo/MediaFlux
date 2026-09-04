from __future__ import annotations

import unittest

from app.agent.confirmation import ConfirmationStore
from app.agent.kernel.capabilities import ToolEffect
from app.agent.kernel.effects import (
    ConfirmationEffectPlanStore,
    EffectPlanError,
    PreparedEffect,
)
from app.agent.kernel.lifecycle import AgentSessionLifecycle
from app.agent.kernel.state import (
    PublicationLease,
    SessionBusyError,
    SessionState,
    TurnCoordinator,
)
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
            session=session,
            store=store,
            effect_store=effects,
            clear_provider_state=lambda *, owner, session_id: provider_clears.append(
                (owner, session_id)
            ),
        )

        state = await lifecycle.reset(owner="owner", session_id="session-a")

        self.assertTrue(token.cancelled)
        self.assertEqual(state.generation, 5)
        self.assertEqual(store.calls, [("reset", "owner", "session-a")])
        self.assertEqual(provider_clears, [("owner", "session-a")])
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
            session=session,
            store=store,
            effect_store=effects,
            clear_provider_state=lambda *, owner, session_id: provider_clears.append(
                (owner, session_id)
            ),
        )

        self.assertTrue(await lifecycle.delete(owner="owner", session_id="session-a"))
        self.assertEqual(store.calls, [("delete", "owner", "session-a")])
        self.assertEqual(provider_clears, [("owner", "session-a")])

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
            session=session,
            store=store,
            effect_store=effects,
            clear_provider_state=lambda *, owner, session_id: provider_clears.append(
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
