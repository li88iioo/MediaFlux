from __future__ import annotations

import asyncio
import json
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet

from app.agent.effect_completion import (
    EffectCompletionScope,
    current_session_effect_trackers,
    wait_for_effect_completion,
)
from app.agent.kernel.state import (
    InMemorySessionStateStore,
    PublicationLease,
    SessionBusyError,
    StateUpdate,
    TurnCoordinator,
)
from app.agent.models import ToolContext, ToolResult
from app.agent.task_stop import stop_agent_session


class _Session:
    def __init__(self, store: InMemorySessionStateStore) -> None:
        self.state_store = store
        self.coordinator = TurnCoordinator()
        self._start_lock = asyncio.Lock()
        self.cancel_calls: list[tuple[str, str, str]] = []
        self.cancel_effect_calls: list[tuple[str, str, str]] = []

    async def cancel(
        self, *, owner: str, session_id: str, request_id: str = ""
    ) -> bool:
        self.cancel_calls.append((owner, session_id, request_id))
        return await self.coordinator.cancel(
            owner=owner, session_id=session_id, request_id=request_id
        )

    async def cancel_effect(
        self, *, owner: str, session_id: str, plan_id: str, request_id: str = ""
    ) -> bool:
        self.cancel_effect_calls.append((owner, session_id, plan_id))
        state = await self.state_store.load(owner=owner, session_id=session_id)
        lease = PublicationLease(
            owner, session_id, state.generation, "stop-test", request_id or "stop-test"
        )
        await self.state_store.commit(
            lease,
            updates=(StateUpdate("pending_effect_plan_id", plan_id, mode="clear_if_equals"),),
        )
        return True


class AgentTaskStopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.store = InMemorySessionStateStore()
        self.session = _Session(self.store)
        self.runtime = SimpleNamespace(session=self.session, store=self.store)
        self.guard = patch(
            "app.agent.task_stop.session_scope_guard",
            side_effect=lambda *args, **kwargs: nullcontext(),
        )
        self.guard.start()
        self.addCleanup(self.guard.stop)

    async def _start_local_waiter(
        self, owner: str, session_id: str, *, protected: bool = False
    ) -> tuple[asyncio.Task[None], asyncio.Event, Any]:
        lease, _ = await self.store.begin_turn(
            owner=owner, session_id=session_id, request_id=f"request:{session_id}"
        )
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def local_work() -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                stopped.set()
                raise

        task = asyncio.create_task(local_work())
        token = await self.session.coordinator.begin(
            lease, protected=protected, task=task
        )
        token.interruptible = True
        await started.wait()
        return task, stopped, token

    async def test_stops_only_requested_session_and_repeats_idempotently(self) -> None:
        task_a, stopped_a, token_a = await self._start_local_waiter("owner", "a")
        task_b, stopped_b, token_b = await self._start_local_waiter("owner", "b")
        state = await self.store.load(owner="owner", session_id="a")
        await self.store.commit(
            PublicationLease("owner", "a", state.generation, "seed", "seed"),
            updates=(StateUpdate("pending_effect_plan_id", "plan-a"),),
        )

        with patch("app.agent.task_stop.current_session_effect_trackers", return_value=[]):
            first = await stop_agent_session(self.runtime, "owner", "a")

        self.assertEqual(first["status"], "stopping")
        self.assertFalse(first["stopped"])
        self.assertEqual(first["confirmation"], "revoked")
        self.assertTrue(token_a.cancelled)
        self.assertFalse(token_b.cancelled)
        self.assertEqual(self.session.cancel_effect_calls, [("owner", "a", "plan-a")])
        self.assertFalse(stopped_b.is_set())
        with self.assertRaises(asyncio.CancelledError):
            await task_a
        self.assertTrue(stopped_a.is_set())

        repeated = await stop_agent_session(self.runtime, "owner", "a")
        self.assertEqual(repeated["status"], "stopping")
        self.assertFalse(repeated["stopped"])
        self.assertEqual(len(self.session.cancel_effect_calls), 1)
        self.assertFalse(token_b.cancelled)
        task_b.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task_b

    async def test_busy_scope_still_signals_local_turn_without_revoking_card(self) -> None:
        task, _stopped, token = await self._start_local_waiter("owner", "busy")
        state = await self.store.load(owner="owner", session_id="busy")
        await self.store.commit(
            PublicationLease("owner", "busy", state.generation, "seed", "seed"),
            updates=(StateUpdate("pending_effect_plan_id", "busy-plan"),),
        )

        with patch(
            "app.agent.task_stop.session_scope_guard", side_effect=SessionBusyError
        ), patch("app.agent.task_stop.current_session_effect_trackers", return_value=[]):
            result = await stop_agent_session(self.runtime, "owner", "busy")

        state = await self.store.load(owner="owner", session_id="busy")
        self.assertEqual(result["status"], "stopping")
        self.assertFalse(result["stopped"])
        self.assertEqual(result["model_turn"], "stop_requested")
        self.assertEqual(result["confirmation"], "pending")
        self.assertTrue(token.cancelled)
        self.assertEqual(state.pending_effect_plan_id, "busy-plan")
        self.assertEqual(state.metadata["stop_requested_generation"], state.generation)
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_protected_write_records_generation_without_claiming_stopped(self) -> None:
        task, _stopped, token = await self._start_local_waiter(
            "owner", "critical", protected=True
        )
        with patch("app.agent.task_stop.current_session_effect_trackers", return_value=[]):
            result = await stop_agent_session(self.runtime, "owner", "critical")

        state = await self.store.load(owner="owner", session_id="critical")
        self.assertEqual(result["status"], "critical_pending")
        self.assertFalse(result["stopped"])
        self.assertEqual(result["model_turn"], "critical_pending")
        self.assertEqual(
            state.metadata["stop_requested_generation"], state.generation
        )
        self.assertFalse(token.cancelled)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_protected_turn_unprotected_before_stop_marker_is_cancelled_by_request_id(self) -> None:
        task, _stopped, token = await self._start_local_waiter(
            "owner", "protect-race", protected=True
        )
        active = await self.session.coordinator.describe(
            owner="owner", session_id="protect-race"
        )
        self.assertIsNotNone(active)
        self.assertEqual(active["request_id"], "request:protect-race")
        marker_update_started = asyncio.Event()
        allow_marker_update = asyncio.Event()
        update_effect_state = self.store.update_effect_state

        async def gated_update_effect_state(*, owner, session_id, change):
            marker_update_started.set()
            await allow_marker_update.wait()
            return await update_effect_state(
                owner=owner, session_id=session_id, change=change
            )

        with patch.object(
            self.store, "update_effect_state", new=gated_update_effect_state
        ), patch("app.agent.task_stop.current_session_effect_trackers", return_value=[]):
            stopping = asyncio.create_task(
                stop_agent_session(self.runtime, "owner", "protect-race")
            )
            await marker_update_started.wait()

            # Simulate the kernel's post-receipt marker check winning the race,
            # followed by unprotect/model continuation before stop writes its marker.
            checked_state = await self.store.load(
                owner="owner", session_id="protect-race"
            )
            self.assertNotEqual(
                checked_state.metadata.get("stop_requested_generation"),
                active["generation"],
            )
            lease = PublicationLease(
                owner="owner",
                session_id="protect-race",
                generation=active["generation"],
                turn_id=active["turn_id"],
                request_id=active["request_id"],
            )
            await self.session.coordinator.unprotect(lease, token)
            allow_marker_update.set()
            result = await stopping

        self.assertEqual(result["status"], "stopping")
        self.assertFalse(result["stopped"])
        self.assertEqual(result["model_turn"], "stop_requested")
        self.assertEqual(
            result["stop_requested_generation"], active["generation"]
        )
        self.assertEqual(
            self.session.cancel_calls,
            [("owner", "protect-race", "request:protect-race")],
        )
        self.assertTrue(token.cancelled)
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_marker_cas_and_request_id_do_not_cancel_later_generation(self) -> None:
        old_task, _stopped, old_token = await self._start_local_waiter(
            "owner", "generation-race", protected=True
        )
        marker_update_started = asyncio.Event()
        allow_marker_update = asyncio.Event()
        update_effect_state = self.store.update_effect_state

        async def gated_update_effect_state(*, owner, session_id, change):
            marker_update_started.set()
            await allow_marker_update.wait()
            return await update_effect_state(
                owner=owner, session_id=session_id, change=change
            )

        with patch.object(
            self.store, "update_effect_state", new=gated_update_effect_state
        ), patch("app.agent.task_stop.current_session_effect_trackers", return_value=[]):
            stopping = asyncio.create_task(
                stop_agent_session(self.runtime, "owner", "generation-race")
            )
            await marker_update_started.wait()

            old_active = await self.session.coordinator.describe(
                owner="owner", session_id="generation-race"
            )
            self.assertIsNotNone(old_active)
            old_lease = PublicationLease(
                owner="owner",
                session_id="generation-race",
                generation=old_active["generation"],
                turn_id=old_active["turn_id"],
                request_id=old_active["request_id"],
            )
            await self.session.coordinator.unprotect(old_lease, old_token)

            new_lease, _ = await self.store.begin_turn(
                owner="owner", session_id="generation-race", request_id="request:new"
            )
            new_task = asyncio.create_task(asyncio.Event().wait())
            new_token = await self.session.coordinator.begin(
                new_lease, protected=False, task=new_task
            )
            new_token.interruptible = True
            allow_marker_update.set()
            result = await stopping

        current = await self.store.load(owner="owner", session_id="generation-race")
        self.assertEqual(result["status"], "superseded")
        self.assertFalse(result["stopped"])
        self.assertEqual(result["model_turn"], "superseded")
        self.assertIsNone(current.metadata.get("stop_requested_generation"))
        self.assertFalse(new_token.cancelled)
        self.assertEqual(
            self.session.cancel_calls,
            [("owner", "generation-race", "request:generation-race")],
        )
        new_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await new_task
        with self.assertRaises(asyncio.CancelledError):
            await old_task

    async def test_tracker_helper_decrypts_only_matching_session_seed(self) -> None:
        cipher = Fernet(Fernet.generate_key())
        plan_a = "plan-a"
        plan_b = "plan-b"

        def sealed(owner: str, session_id: str, plan_id: str) -> str:
            return cipher.encrypt(
                json.dumps(
                    {
                        "owner": owner,
                        "session_id": session_id,
                        "request_id": "request",
                        "turn_id": "turn",
                        "plan_id": plan_id,
                        "channel": "test",
                        "tool": "test.tool",
                        "tracker": {
                            "kind": "agent_job",
                            "value": {"job_id": "job_stable_1234567890"},
                        },
                        "result": {},
                    }
                ).encode()
            ).decode()

        await self.store.begin_turn(owner="owner", session_id="a", request_id="a")
        await self.store.begin_turn(owner="owner", session_id="b", request_id="b")
        await self.store.update_effect_state(
            owner="owner",
            session_id="a",
            change=lambda state: state.metadata.update(
                effect_waits={
                    plan_a: {
                        "sealed": sealed("owner", "a", plan_a),
                        "state": "pending",
                        "last_status": "running",
                        "delivered": False,
                    },
                    # Even authenticated ciphertext stored in A cannot point to B.
                    plan_b: {
                        "sealed": sealed("owner", "b", plan_b),
                        "state": "pending",
                        "last_status": "running",
                        "delivered": False,
                    },
                }
            ),
        )

        with patch("app.agent.effect_completion._completion_cipher", return_value=cipher):
            records = await current_session_effect_trackers(
                self.store, owner="owner", session_id="a"
            )

        self.assertEqual([record["plan_id"] for record in records], [plan_a])
        self.assertEqual(records[0]["owner"], "owner")
        self.assertEqual(records[0]["session_id"], "a")
        self.assertEqual(records[0]["tracker"]["value"]["job_id"], "job_stable_1234567890")
        # The helper is read-only: both encrypted wait records remain for receipt tracking.
        state = await self.store.load(owner="owner", session_id="a")
        self.assertEqual(set(state.metadata["effect_waits"]), {plan_a, plan_b})

    async def test_completion_wait_observes_generation_stop_without_dropping_tracker(self) -> None:
        lease, _ = await self.store.begin_turn(
            owner="owner", session_id="wait", request_id="request"
        )
        result = ToolResult(
            True,
            "accepted",
            "云端操作已提交",
            effect_metadata={
                "completion": {
                    "kind": "agent_job",
                    "job_id": "job_abcdefghijklmnop",
                }
            },
        )
        await self.store.update_effect_state(
            owner="owner",
            session_id="wait",
            change=lambda state: state.metadata.update(
                stop_requested_generation=lease.generation
            ),
        )
        cipher = Fernet(Fernet.generate_key())
        scope = EffectCompletionScope(
            store=self.store, lease=lease, plan_id="wait-plan", channel="test"
        )
        context = ToolContext(owner="owner", session_id="wait", request_id="request")
        with patch("app.agent.effect_completion._completion_cipher", return_value=cipher), patch(
            "app.agent.effect_completion._poll",
            new=AsyncMock(return_value=(
                ToolResult(True, "running", "仍在执行"),
                "running",
                {"status": "running"},
            )),
        ) as poll:
            pending = await wait_for_effect_completion(
                result, tool="test.tool", context=context, timeout_seconds=60, scope=scope
            )

        self.assertEqual(pending.status, "running")
        self.assertTrue(pending.data["background_job"]["followup_pending"])
        poll.assert_awaited_once()
        state = await self.store.load(owner="owner", session_id="wait")
        wait_record = state.metadata["effect_waits"]["wait-plan"]
        self.assertEqual(wait_record["state"], "pending")
        self.assertEqual(wait_record["last_status"], "running")
        self.assertIn("sealed", wait_record)

    async def test_unavailable_cloud_task_is_reported_uncancellable_and_retained(self) -> None:
        await self.store.begin_turn(owner="owner", session_id="cloud", request_id="cloud")
        cloud_tracker = {
            "owner": "owner",
            "session_id": "cloud",
            "plan_id": "cloud-plan",
            "tracker": {
                "kind": "guangya_task",
                "value": {"task_id": "provider-stable-id"},
            },
            "state": "pending",
            "last_status": "running",
            "delivered": False,
        }
        with patch(
            "app.agent.task_stop.current_session_effect_trackers",
            return_value=[cloud_tracker],
        ):
            result = await stop_agent_session(self.runtime, "owner", "cloud")

        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["stopped"])
        self.assertEqual(result["uncancellable"], [{
            "plan_id": "cloud-plan",
            "kind": "guangya_task",
            "status": "uncancellable",
        }])

    async def test_agent_job_cancel_is_owner_and_task_id_scoped_and_reports_stopping(self) -> None:
        await self.store.begin_turn(owner="owner", session_id="job", request_id="job")
        job_id = "job_abcdefghijklmnop"
        tracker = {
            "owner": "owner",
            "session_id": "job",
            "plan_id": "job-plan",
            "tracker": {"kind": "agent_job", "value": {"job_id": job_id}},
            "state": "pending",
            "last_status": "running",
            "delivered": False,
        }
        row = {"job_id": job_id, "job_type": "library_episode_audit", "status": "running"}
        with patch(
            "app.agent.task_stop.current_session_effect_trackers", return_value=[tracker]
        ), patch(
            "app.repositories.agent_jobs.get_agent_job", return_value=row
        ) as get_job, patch(
            "app.repositories.agent_jobs.cancel_agent_job", return_value=(row, "requested")
        ) as cancel_job, patch(
            "app.modules.agent_jobs_scheduler.get_agent_jobs_scheduler"
        ) as scheduler_factory:
            result = await stop_agent_session(self.runtime, "owner", "job")

        get_job.assert_called_once_with(owner="owner", job_id=job_id)
        cancel_job.assert_called_once_with(
            owner="owner", job_id=job_id, job_type="library_episode_audit"
        )
        scheduler_factory.return_value.wake.assert_called_once_with()
        self.assertEqual(result["status"], "stopping")
        self.assertFalse(result["stopped"])

    async def test_persistent_organize_operation_uses_owner_bound_native_cancel(self) -> None:
        await self.store.begin_turn(owner="owner", session_id="operation", request_id="operation")
        tracker = {
            "owner": "owner",
            "session_id": "operation",
            "plan_id": "operation-plan",
            "tracker": {
                "kind": "guangya_operation",
                "value": {"operation_ref": "GY-1111-1111-1111-1111-1111-1111-1111"},
            },
            "state": "pending",
            "last_status": "running",
            "delivered": False,
        }
        row = {"job_id": "1" * 32, "lease_generation": 3, "status": "running"}
        with patch(
            "app.agent.task_stop.current_session_effect_trackers",
            return_value=[tracker],
        ), patch(
            "app.repositories.organize_operation_jobs.organize_operation_job_id_from_public_ref",
            return_value="1" * 32,
        ) as parse_ref, patch(
            "app.repositories.organize_operation_jobs.get_organize_operation_job_for_owner",
            return_value=row,
        ) as get_for_owner, patch(
            "app.repositories.organize_operation_jobs.organize_operation_public_ref",
            return_value="GY-1111-1111-1111-1111-1111-1111-1111",
        ), patch(
            "app.repositories.organize_operation_jobs.request_cancel_organize_operation_job",
            return_value=(row, "requested"),
        ) as request_cancel, patch(
            "app.modules.organize_tasks.get_organize_manager",
        ) as manager_factory:
            manager_factory.return_value._operation_queue_wakeup = asyncio.Event()
            outcome = await stop_agent_session(self.runtime, "owner", "operation")

        parse_ref.assert_called_once_with("GY-1111-1111-1111-1111-1111-1111-1111")
        get_for_owner.assert_called_once_with("1" * 32, "owner")
        request_cancel.assert_called_once_with(
            "1" * 32, owner="owner", expected_lease_generation=3
        )
        self.assertTrue(manager_factory.return_value._operation_queue_wakeup.is_set())
        self.assertEqual(outcome["status"], "stopping")
        self.assertFalse(outcome["stopped"])
        self.assertEqual(outcome["background_tasks"][0]["status"], "cancel_requested")

    async def test_accepted_cloud_copy_operation_stays_uncancellable_and_tracked(self) -> None:
        await self.store.begin_turn(owner="owner", session_id="copy", request_id="copy")
        tracker = {
            "owner": "owner",
            "session_id": "copy",
            "plan_id": "copy-plan",
            "tracker": {
                "kind": "guangya_operation",
                "value": {"operation_ref": "GY-2222-2222-2222-2222-2222-2222-2222"},
            },
            "state": "pending",
            "last_status": "running",
            "delivered": False,
        }
        row = {"job_id": "2" * 32, "lease_generation": 4, "status": "pending"}
        with patch(
            "app.agent.task_stop.current_session_effect_trackers", return_value=[tracker]
        ), patch(
            "app.repositories.organize_operation_jobs.organize_operation_job_id_from_public_ref",
            return_value="2" * 32,
        ), patch(
            "app.repositories.organize_operation_jobs.get_organize_operation_job_for_owner",
            return_value=row,
        ), patch(
            "app.repositories.organize_operation_jobs.organize_operation_public_ref",
            return_value="GY-2222-2222-2222-2222-2222-2222-2222",
        ), patch(
            "app.repositories.organize_operation_jobs.request_cancel_organize_operation_job",
            return_value=(row, "uncancellable"),
        ) as request_cancel, patch(
            "app.modules.organize_tasks.get_organize_manager"
        ) as manager_factory:
            result = await stop_agent_session(self.runtime, "owner", "copy")

        request_cancel.assert_called_once_with(
            "2" * 32, owner="owner", expected_lease_generation=4
        )
        manager_factory.assert_not_called()
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["stopped"])
        self.assertEqual(result["uncancellable"][0]["plan_id"], "copy-plan")


if __name__ == "__main__":
    unittest.main()
