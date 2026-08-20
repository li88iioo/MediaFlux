"""Agent 操作协调器的并发与取消语义。"""
from __future__ import annotations

import threading
import unittest

from app.agent.operation_coordinator import (
    AgentOperationCoordinator,
    RecentEventDeduplicator,
)


class AgentOperationCoordinatorTests(unittest.TestCase):
    def test_new_operation_supersedes_previous_lease(self):
        coordinator = AgentOperationCoordinator()
        first = coordinator.begin(owner="owner-a", operation_id="first")
        second = coordinator.begin(owner="owner-a", operation_id="second")

        self.assertFalse(coordinator.is_current(first))
        self.assertEqual(coordinator.reason(first), "superseded")
        self.assertTrue(coordinator.is_current(second))

    def test_pre_cancelled_operation_never_becomes_current(self):
        coordinator = AgentOperationCoordinator()
        self.assertTrue(
            coordinator.cancel(
                owner="owner-a", operation_id="late-request", reason="user_cancelled"
            )
        )

        lease = coordinator.begin(owner="owner-a", operation_id="late-request")

        self.assertFalse(coordinator.is_current(lease))
        self.assertEqual(coordinator.reason(lease), "user_cancelled")

    def test_finalize_has_one_linearized_winner(self):
        coordinator = AgentOperationCoordinator()
        lease = coordinator.begin(owner="owner-a", operation_id="request-a")
        published: list[str] = []

        first, _ = coordinator.finalize_if_current(
            lease, lambda: published.append("final")
        )
        second, _ = coordinator.finalize_if_current(
            lease, lambda: published.append("duplicate")
        )

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(published, ["final"])

    def test_cancel_waits_for_same_owner_publication(self):
        coordinator = AgentOperationCoordinator()
        lease = coordinator.begin(owner="owner-a", operation_id="request-a")
        entered = threading.Event()
        release = threading.Event()
        cancelled = threading.Event()

        def publish() -> None:
            entered.set()
            release.wait(timeout=2)

        publish_thread = threading.Thread(
            target=lambda: coordinator.publish_if_current(lease, publish)
        )
        cancel_thread = threading.Thread(
            target=lambda: (
                coordinator.cancel(owner="owner-a", operation_id="request-a"),
                cancelled.set(),
            )
        )
        publish_thread.start()
        self.assertTrue(entered.wait(timeout=1))
        cancel_thread.start()
        self.assertFalse(cancelled.wait(timeout=0.05))
        release.set()
        publish_thread.join(timeout=1)
        cancel_thread.join(timeout=1)
        self.assertTrue(cancelled.is_set())
        self.assertFalse(coordinator.is_current(lease))

    def test_cancel_waits_until_publication_window_exits(self):
        coordinator = AgentOperationCoordinator()
        lease = coordinator.begin(owner="owner-a", operation_id="request-a")
        entered = threading.Event()
        release = threading.Event()
        cancelled = threading.Event()

        def hold_publication_window() -> None:
            with coordinator.publication_window_if_current(lease) as allowed:
                self.assertTrue(allowed)
                entered.set()
                self.assertTrue(release.wait(timeout=2))

        publisher = threading.Thread(target=hold_publication_window)
        canceller = threading.Thread(
            target=lambda: (
                coordinator.cancel(owner="owner-a", operation_id="request-a"),
                cancelled.set(),
            )
        )
        publisher.start()
        self.assertTrue(entered.wait(timeout=1))
        canceller.start()
        self.assertFalse(cancelled.wait(timeout=0.05))

        release.set()
        publisher.join(timeout=1)
        canceller.join(timeout=1)

        self.assertFalse(publisher.is_alive())
        self.assertFalse(canceller.is_alive())
        self.assertTrue(cancelled.is_set())
        self.assertFalse(coordinator.is_current(lease))

    def test_new_begin_waits_until_publication_window_exits(self):
        coordinator = AgentOperationCoordinator()
        first = coordinator.begin(owner="owner-a", operation_id="first")
        entered = threading.Event()
        release = threading.Event()
        began = threading.Event()
        leases: list = []

        def hold_publication_window() -> None:
            with coordinator.publication_window_if_current(first) as allowed:
                self.assertTrue(allowed)
                entered.set()
                self.assertTrue(release.wait(timeout=2))

        def begin_new() -> None:
            leases.append(coordinator.begin(owner="owner-a", operation_id="second"))
            began.set()

        publisher = threading.Thread(target=hold_publication_window)
        starter = threading.Thread(target=begin_new)
        publisher.start()
        self.assertTrue(entered.wait(timeout=1))
        starter.start()
        self.assertFalse(began.wait(timeout=0.05))

        release.set()
        publisher.join(timeout=1)
        starter.join(timeout=1)

        self.assertEqual(len(leases), 1)
        self.assertEqual(coordinator.reason(first), "superseded")
        self.assertTrue(coordinator.is_current(leases[0]))

    def test_cancel_waits_until_finalization_window_exits(self):
        coordinator = AgentOperationCoordinator()
        lease = coordinator.begin(owner="owner-a", operation_id="request-a")
        entered = threading.Event()
        release = threading.Event()
        cancelled = threading.Event()

        def finalize() -> None:
            with coordinator.finalization_window_if_current(lease) as allowed:
                self.assertTrue(allowed)
                entered.set()
                self.assertTrue(release.wait(timeout=2))

        finalizer = threading.Thread(target=finalize)
        canceller = threading.Thread(
            target=lambda: (
                coordinator.cancel(owner="owner-a", operation_id="request-a"),
                cancelled.set(),
            )
        )
        finalizer.start()
        self.assertTrue(entered.wait(timeout=1))
        canceller.start()
        self.assertFalse(cancelled.wait(timeout=0.05))

        release.set()
        finalizer.join(timeout=1)
        canceller.join(timeout=1)

        self.assertFalse(finalizer.is_alive())
        self.assertFalse(canceller.is_alive())
        self.assertTrue(cancelled.is_set())
        self.assertFalse(coordinator.is_current(lease))

    def test_invalidate_owner_waits_until_owner_window_exits(self):
        coordinator = AgentOperationCoordinator()
        entered = threading.Event()
        release = threading.Event()
        invalidated = threading.Event()
        callbacks: list[str] = []

        def hold_owner_window() -> None:
            with coordinator.owner_window("owner-a"):
                entered.set()
                self.assertTrue(release.wait(timeout=2))

        def invalidate_owner() -> None:
            coordinator.invalidate_owner(
                owner="owner-a",
                invalidate=lambda: callbacks.append("invalidated"),
            )
            invalidated.set()

        holder = threading.Thread(target=hold_owner_window)
        invalidator = threading.Thread(target=invalidate_owner)
        holder.start()
        self.assertTrue(entered.wait(timeout=1))
        invalidator.start()
        self.assertFalse(invalidated.wait(timeout=0.05))

        release.set()
        holder.join(timeout=1)
        invalidator.join(timeout=1)

        self.assertFalse(holder.is_alive())
        self.assertFalse(invalidator.is_alive())
        self.assertTrue(invalidated.is_set())
        self.assertEqual(callbacks, ["invalidated"])

    def test_finished_owners_do_not_accumulate_generation_metadata(self):
        coordinator = AgentOperationCoordinator()

        for index in range(4096):
            lease = coordinator.begin(
                owner=f"owner-{index}",
                operation_id=f"request-{index}",
            )
            coordinator.finish(lease)

        self.assertEqual(len(coordinator._generations), 0)
        self.assertEqual(len(coordinator._owner_locks), 64)

    def test_reset_waits_for_publication_window_and_clears_state(self):
        coordinator = AgentOperationCoordinator()
        lease = coordinator.begin(owner="owner-a", operation_id="request-a")
        entered = threading.Event()
        release = threading.Event()
        reset_done = threading.Event()

        def publish() -> None:
            with coordinator.publication_window_if_current(lease) as allowed:
                self.assertTrue(allowed)
                entered.set()
                self.assertTrue(release.wait(timeout=2))

        publisher = threading.Thread(target=publish)
        resetter = threading.Thread(
            target=lambda: (coordinator.reset(), reset_done.set())
        )
        publisher.start()
        self.assertTrue(entered.wait(timeout=1))
        resetter.start()
        self.assertFalse(reset_done.wait(timeout=0.05))

        release.set()
        publisher.join(timeout=1)
        resetter.join(timeout=1)

        self.assertTrue(reset_done.is_set())
        self.assertFalse(coordinator.is_current(lease))
        replacement = coordinator.begin(owner="owner-a", operation_id="replacement")
        self.assertTrue(coordinator.is_current(replacement))


class RecentEventDeduplicatorTests(unittest.TestCase):
    def test_claim_is_idempotent_until_ttl_expires(self):
        now = [10.0]
        store: RecentEventDeduplicator[str] = RecentEventDeduplicator(
            ttl_seconds=5, clock=lambda: now[0]
        )
        self.assertTrue(store.claim("message-1"))
        self.assertFalse(store.claim("message-1"))
        now[0] = 16.0
        self.assertTrue(store.claim("message-1"))


if __name__ == "__main__":
    unittest.main()
