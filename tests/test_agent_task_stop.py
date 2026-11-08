from __future__ import annotations

import asyncio
import json
import unittest
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet

from app.agent.effect_completion import (
    session_effect_trackers,
    wait_for_effect_completion,
)
from app.agent.feature_gate import initialize_runtime_fence
from app.agent.kernel.bootstrap import _capture_creation_binding
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.completion_receipts import (
    EffectCompletionScope,
    _completion_cipher,
)
from app.agent.kernel.continuation import (
    _ContinuationAction,
    register_effect,
    release_effect,
)
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.session import AgentSession
from app.agent.kernel.session_guard import session_scope_guard
from app.agent.kernel.state import InMemorySessionStateStore, TurnCoordinator
from app.agent.kernel.state_types import (
    CancellationToken,
    PublicationLease,
    SessionBusyError,
    StalePublicationError,
    StateUpdate,
    _ConfirmationClaim,
    _PublicationUnprotected,
)
from app.agent.models import ToolContext, ToolResult
from app.agent.task_stop import stop_agent_session
from tests.support import isolated_test_database


class _Session:
    def __init__(self, store: InMemorySessionStateStore) -> None:
        self.state_store = store
        self.coordinator = TurnCoordinator()
        self._start_lock = asyncio.Lock()
        self.cancel_calls: list[tuple[str, str, str]] = []
        self.cancel_effect_calls: list[tuple[str, str, str]] = []

    async def activity(self, *, owner: str, session_id: str):
        return await self.coordinator.describe(owner=owner, session_id=session_id)

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

    async def test_queued_interrupt_cannot_cancel_model_cancellation_settlement(self):
        token = CancellationToken()
        token.interruptible = True
        token.cancel("user_cancelled", asyncio.current_task())
        # 模型先观察到协作取消，离开可打断阶段；排队的强制取消还未交付。
        with self.assertRaises(asyncio.CancelledError):
            token.raise_if_cancelled()
        token.interruptible = False
        # 真实事件循环交付此前回调，不可再次打断终态保存。
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertTrue(token.cancelled)
        self.assertEqual(token.reason, "user_cancelled")

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

        with patch("app.agent.task_stop.session_effect_trackers", return_value=[]):
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
        ), patch("app.agent.task_stop.session_effect_trackers", return_value=[]):
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
        with patch("app.agent.task_stop.session_effect_trackers", return_value=[]):
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
        assert (active) is not None
        self.assertEqual(active["request_id"], "request:protect-race")
        marker_update_started = asyncio.Event()
        allow_marker_update = asyncio.Event()
        request_stop_generation = self.store.request_stop_generation

        async def gated_request_stop_generation(*, owner, session_id, generation):
            marker_update_started.set()
            await allow_marker_update.wait()
            return await request_stop_generation(
                owner=owner, session_id=session_id, generation=generation,
            )

        with patch.object(
            self.store, "request_stop_generation", new=gated_request_stop_generation
        ), patch("app.agent.task_stop.session_effect_trackers", return_value=[]):
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
        request_stop_generation = self.store.request_stop_generation

        async def gated_request_stop_generation(*, owner, session_id, generation):
            marker_update_started.set()
            await allow_marker_update.wait()
            return await request_stop_generation(
                owner=owner, session_id=session_id, generation=generation,
            )

        with patch.object(
            self.store, "request_stop_generation", new=gated_request_stop_generation
        ), patch("app.agent.task_stop.session_effect_trackers", return_value=[]):
            stopping = asyncio.create_task(
                stop_agent_session(self.runtime, "owner", "generation-race")
            )
            await marker_update_started.wait()

            old_active = await self.session.coordinator.describe(
                owner="owner", session_id="generation-race"
            )
            assert (old_active) is not None
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
        # CAS 已失效，不再向可能复用 source request_id 的新续行发取消。
        self.assertEqual(self.session.cancel_calls, [])
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

        with patch("app.agent.kernel.completion_receipts._completion_cipher", return_value=cipher):
            snapshot = await self.store.load(owner="owner", session_id="a")
            records = session_effect_trackers(snapshot)

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
        with patch("app.agent.kernel.completion_receipts._completion_cipher", return_value=cipher), patch(
            "app.agent.completion_observers._poll",
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
            "app.agent.task_stop.session_effect_trackers",
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
            "app.agent.task_stop.session_effect_trackers", return_value=[tracker]
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
            "app.agent.task_stop.session_effect_trackers",
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
            "app.agent.task_stop.session_effect_trackers", return_value=[tracker]
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


class SQLiteAgentTaskStopTests(unittest.IsolatedAsyncioTestCase):
    """使用真实签名状态、文件互斥和原生 Session，不能用内存替身掩盖竞争。"""

    async def asyncSetUp(self) -> None:
        database = isolated_test_database()
        database.__enter__()
        self.addCleanup(database.__exit__, None, None, None)
        initialize_runtime_fence()
        self.binding = _capture_creation_binding()
        self.store = self._cold_store()
        self.cipher = _completion_cipher()
        self.model = AsyncMock()
        self.runtime = self._runtime(self.store)

    def _cold_store(self) -> SQLiteKernelStore:
        return SQLiteKernelStore(secret_provider=lambda: "stop-sqlite-secret", clock=lambda: 100)

    def _runtime(self, store: SQLiteKernelStore) -> Any:
        catalog = ToolCatalog([])
        session = AgentSession(
            model=self.model, catalog=catalog, model_tools=(),
            pipeline=ToolPipeline(catalog=catalog, state_store=store), state_store=store,
        )
        session.continuation_binding = self.binding
        return SimpleNamespace(session=session, store=store)

    async def _seed_effect(
        self, session_id: str, *, owner: str = "owner", finished: bool = False, channel: str = "web",
    ) -> tuple[PublicationLease, str]:
        lease, _ = await self.store.begin_turn(
            owner=owner, session_id=session_id, request_id=f"source:{session_id}",
        )
        plan_id = f"plan:{session_id}"
        await self.store.commit(lease, updates=(StateUpdate("pending_effect_plan_id", plan_id),))
        await self.store.commit(lease, updates=(
            _ConfirmationClaim("", {
                "generation": lease.generation, "turn_id": lease.turn_id, "plan_id": plan_id,
            }, request_id=lease.request_id),
        ))
        await self.store.commit(lease, updates=(_PublicationUnprotected("", None),))
        origin = {
            "goal": {"role": "user", "content": "完成已确认操作后继续读取实际结果"},
            "arguments": {}, "owner": owner, "session_id": session_id,
            "source_request_id": lease.request_id, "source_turn_id": lease.turn_id,
            "origin_generation": lease.generation, "expected_task": None,
            "admission_fence": asdict(self.binding.fence),
            "effective_digest": self.binding.effective_digest,
            "model_policy": {"fixture": "stop"}, "channel": channel,
            "model_limit": 12, "tool_limit": 16,
        }
        seed = {
            "owner": owner, "session_id": session_id, "plan_id": plan_id,
            "tool": "test.effect", "tracker": {"kind": "guangya_task", "value": {"task_id": plan_id}},
            "origin": origin,
        }
        row: dict[str, Any] = {
            "sealed": self.cipher.encrypt(json.dumps(seed).encode()).decode(),
            "state": "terminal" if finished else "pending",
            "last_status": "completed" if finished else "running",
            "delivered": finished, "next_poll_at": 100, "expires_at": 1000,
        }
        register_effect(row, EffectCompletionScope(
            store=self.store, lease=lease, plan_id=plan_id, channel=channel, origin=origin,
        ), 100)
        if finished:
            row["final_result"] = {"ok": True, "status": "completed", "summary": "云端已完成"}
            release_effect(row, 100)
        await self.store.update_effect_state(
            owner=owner, session_id=session_id,
            change=lambda state: state.metadata.update(effect_waits={plan_id: row}, effect_next_poll_at=100),
        )
        return lease, plan_id

    async def test_cold_stop_revokes_all_effect_continuation_states_and_stale_leases(self) -> None:
        for phase in ("waiting", "pending", "claimed", "running"):
            with self.subTest(phase=phase):
                _, plan_id = await self._seed_effect(phase, finished=phase != "waiting")
                cold = self._cold_store()
                runtime = self._runtime(cold)
                before = await cold.load(owner="owner", session_id=phase)
                candidate = deepcopy(before.metadata["effect_waits"][plan_id]["continuation"])
                resume_lease = None
                if phase in {"claimed", "running"}:
                    resume_lease, _ = await cold.claim_continuation(candidate, self.binding)
                    if phase == "running":
                        await cold.commit(resume_lease, updates=(_ContinuationAction(
                            "metadata.effect_continuation", {
                                "operation": "reserve", "model_limit": 12, "tool_limit": 16,
                                "model_spent": 1, "tool_spent": 0,
                            }, binding=self.binding,
                        ),))
                    self.assertFalse(await runtime.session.cancel(
                        owner="owner", session_id=phase, request_id="another-request",
                    ))
                before = await cold.load(owner="owner", session_id=phase)
                self.assertEqual(before.metadata["effect_waits"][plan_id]["continuation"]["state"], phase)
                self.assertIsNone(await runtime.session.coordinator.describe(owner="owner", session_id=phase))

                result = await stop_agent_session(runtime, "owner", phase)

                after = await self._cold_store().load(owner="owner", session_id=phase)
                expected = deepcopy(before.metadata)
                expected["stop_requested_generation"] = before.generation
                expected["effect_waits"][plan_id]["continuation"].update(
                    state="cancelled", terminal_reason="cancelled",
                )
                self.assertEqual(after.metadata, expected)
                self.assertEqual(after.generation, before.generation)
                self.assertEqual(after.conversation, before.conversation)
                self.assertEqual(result["cancelled_continuations"], [plan_id])
                self.assertEqual(result["model_turn"], "not_running")
                self.assertEqual(result["status"], "partial" if phase == "waiting" else "stopped")
                self.assertEqual(result["stopped"], phase != "waiting")
                if phase == "waiting":
                    self.assertEqual(result["uncancellable"], [{
                        "plan_id": plan_id, "kind": "guangya_task", "status": "uncancellable",
                    }])
                    # 晚到的云端完成回执不能将已取消的 continuation 重新激活。
                    def complete(state, plan_id=plan_id):
                        row = state.metadata["effect_waits"][plan_id]
                        row["final_result"] = {"ok": True, "status": "completed"}
                        release_effect(row, 100)
                    await cold.update_effect_state(owner="owner", session_id=phase, change=complete)
                self.assertIsNone(await cold.claim_continuation(candidate, self.binding))
                self.assertFalse(await runtime.session.resume_wait(candidate=candidate))
                self.assertNotIn(plan_id, [r["wait_id"] for r in await cold.due_continuations()])
                if resume_lease is not None:
                    self.assertFalse(await cold.is_current(resume_lease))
                    with self.assertRaises(StalePublicationError):
                        await cold.commit(resume_lease, updates=(StateUpdate("summary", "不应发布"),))
                repeated = await stop_agent_session(runtime, "owner", phase)
                self.assertEqual(repeated["cancelled_continuations"], [])
        self.model.stream.assert_not_called()

    async def test_terminal_delivery_pending_is_not_reported_or_revoked_as_planning(self) -> None:
        _, plan_id = await self._seed_effect("delivery", finished=True, channel="telegram")
        candidate = (await self.store.due_continuations())[0]
        lease, _ = await self.store.claim_continuation(candidate, self.binding)
        view = {
            "session_id": "delivery", "turn_id": lease.turn_id, "request_id": lease.request_id,
            "status": "success", "answer": "已完成真实操作，保留待投递事实。",
        }
        await self.store.commit(lease, updates=(_ContinuationAction(
            "metadata.effect_continuation", {"operation": "finish", "reason": "completed", "delivery_view": view},
            binding=self.binding,
        ),))
        before = await self.store.load(owner="owner", session_id="delivery")

        result = await stop_agent_session(self.runtime, "owner", "delivery")

        self.assertEqual(result["cancelled_continuations"], [])
        self.assertEqual(result["model_turn"], "not_running")
        self.assertEqual(result["status"], "already_stopped")
        self.assertTrue(result["stopped"])
        self.assertEqual(await self.store.load(owner="owner", session_id="delivery"), before)
        deliveries = await self.store.due_continuations()
        self.assertEqual([r["kind"] for r in deliveries], ["delivery"])
        self.assertEqual(deliveries[0]["delivery_view"], view)
        # 新回合消费历史回执时，也不能提前删掉尚未持久接纳的 Telegram 视图。
        await self.store.begin_turn(owner="owner", session_id="delivery", request_id="new-user")
        after = await self.store.load(owner="owner", session_id="delivery")
        self.assertNotIn(plan_id, after.metadata.get("effect_waits", {}))
        self.assertEqual(after.metadata["continuation_deliveries"], before.metadata["continuation_deliveries"])
        self.model.stream.assert_not_called()

    async def test_cold_stop_does_not_revoke_other_owner_session_or_misplaced_records(self) -> None:
        _, target = await self._seed_effect("target")
        _, foreign = await self._seed_effect("other", owner="other-owner")
        await self._seed_effect("target", owner="other-owner")
        await self._seed_effect("other")
        foreign_state = await self.store.load(owner="other-owner", session_id="other")
        foreign_row = deepcopy(foreign_state.metadata["effect_waits"][foreign])
        def add_records(state):
            state.metadata["effect_waits"][foreign] = foreign_row
            terminal = deepcopy(state.metadata["effect_waits"][target])
            terminal["continuation"].update(state="terminal", terminal_reason="done")
            terminal["continuation"]["wait_id"] = "done"
            state.metadata["effect_waits"]["done"] = terminal
        await self.store.update_effect_state(owner="owner", session_id="target", change=add_records)
        scopes = (("other-owner", "other"), ("other-owner", "target"), ("owner", "other"))
        snapshots = [await self.store.load(owner=o, session_id=s) for o, s in scopes]
        target_before = await self.store.load(owner="owner", session_id="target")

        result = await stop_agent_session(self.runtime, "owner", "target")

        self.assertEqual(result["cancelled_continuations"], [target])
        self.assertEqual([t["plan_id"] for t in result["background_tasks"]], [target])
        target_after = await self.store.load(owner="owner", session_id="target")
        for key in (foreign, "done"):
            self.assertEqual(target_after.metadata["effect_waits"][key], target_before.metadata["effect_waits"][key])
        for (owner, session_id), before in zip(scopes, snapshots):
            self.assertEqual(await self.store.load(owner=owner, session_id=session_id), before)

    async def test_sqlite_marker_cas_does_not_cancel_new_generation_with_same_source_request(self) -> None:
        lease, plan_id = await self._seed_effect("cas", finished=True)
        session = self.runtime.session
        token = await session.coordinator.begin(lease, protected=True)
        entered, release = asyncio.Event(), asyncio.Event()
        request_stop = self.store.request_stop_generation
        async def delayed_request_stop(**kwargs):
            entered.set()
            await release.wait()
            return await request_stop(**kwargs)
        with patch.object(self.store, "request_stop_generation", new=delayed_request_stop), patch.object(
            session, "cancel", wraps=session.cancel,
        ) as cancel:
            stopping = asyncio.create_task(stop_agent_session(self.runtime, "owner", "cas"))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                await session.coordinator.unprotect(lease, token)
                newer, _ = await self.store.begin_turn(
                    owner="owner", session_id="cas", request_id=lease.request_id,
                )
                newer_token = await session.coordinator.begin(newer)
                await self.store.commit(newer, updates=(StateUpdate("pending_effect_plan_id", "new-confirmation"),))
            finally:
                release.set()
            result = await stopping
            cancel.assert_not_called()
        state = await self.store.load(owner="owner", session_id="cas")
        self.assertEqual(result["status"], "superseded")
        self.assertEqual(result["stop_requested_generation"], 0)
        self.assertEqual(result["cancelled_continuations"], [])
        self.assertNotIn("stop_requested_generation", state.metadata)
        self.assertFalse(newer_token.cancelled)
        self.assertTrue(await self.store.is_current(newer))
        self.assertEqual(state.pending_effect_plan_id, "new-confirmation")
        self.assertEqual(state.metadata["effect_waits"][plan_id]["continuation"]["state"], "pending")

    async def test_sqlite_generation_revalidated_after_marker_before_native_cancel(self) -> None:
        lease, _ = await self._seed_effect("after-marker", finished=True)
        session = self.runtime.session
        token = await session.coordinator.begin(lease, protected=True)
        request_stop = self.store.request_stop_generation
        newer_token = None
        async def supersede_after_marker(**kwargs):
            nonlocal newer_token
            marked = await request_stop(**kwargs)
            await session.coordinator.unprotect(lease, token)
            newer, _ = await self.store.begin_turn(
                owner="owner", session_id="after-marker", request_id=lease.request_id,
            )
            newer_token = await session.coordinator.begin(newer)
            await self.store.commit(newer, updates=(StateUpdate("pending_effect_plan_id", "new-card"),))
            return marked
        with patch.object(self.store, "request_stop_generation", new=supersede_after_marker), patch.object(
            session, "cancel", wraps=session.cancel,
        ) as cancel:
            result = await stop_agent_session(self.runtime, "owner", "after-marker")
            cancel.assert_not_called()
        state = await self.store.load(owner="owner", session_id="after-marker")
        self.assertEqual(result["status"], "superseded")
        self.assertFalse(result["stopped"])
        self.assertEqual(result["stop_requested_generation"], lease.generation)
        assert newer_token is not None
        self.assertFalse(newer_token.cancelled)
        self.assertEqual(state.pending_effect_plan_id, "new-card")
        self.assertNotEqual(state.metadata["stop_requested_generation"], state.generation)

    async def _stop_while_effect_guard_held(self, session_id: str) -> dict[str, Any]:
        entered, release = asyncio.Event(), asyncio.Event()
        async def confirm_scope():
            with session_scope_guard("owner", session_id, kind="effect"):
                entered.set()
                await release.wait()
        confirming = asyncio.create_task(confirm_scope())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            return await stop_agent_session(self.runtime, "owner", session_id)
        finally:
            release.set()
            await confirming

    async def test_sqlite_protected_effect_guard_allows_generation_stop_fence(self) -> None:
        lease, plan_id = await self._seed_effect("protected")
        token = await self.runtime.session.coordinator.begin(lease, protected=True)
        before = await self.store.load(owner="owner", session_id="protected")

        result = await self._stop_while_effect_guard_held("protected")

        self.assertEqual(result["status"], "critical_pending")
        self.assertFalse(result["stopped"])
        self.assertEqual(result["stop_requested_generation"], lease.generation)
        self.assertEqual(result["cancelled_continuations"], [])
        self.assertEqual(result["confirmation"], "unchanged")
        self.assertFalse(token.cancelled)
        after = await self.store.load(owner="owner", session_id="protected")
        expected = deepcopy(before.metadata)
        expected["stop_requested_generation"] = lease.generation
        self.assertEqual(after.metadata, expected)
        self.assertEqual(after.generation, before.generation)
        self.assertEqual(after.metadata["confirmed_publication"], before.metadata["confirmed_publication"])
        # 无竞争时原 generation CAS 仍能写入，且不打断确认体。
        retried = await stop_agent_session(self.runtime, "owner", "protected")
        self.assertEqual(retried["stop_requested_generation"], lease.generation)
        state = await self.store.load(owner="owner", session_id="protected")
        self.assertEqual(state.metadata["effect_waits"][plan_id]["continuation"]["state"], "waiting")

    async def test_sqlite_remote_effect_guard_allows_generation_stop_fence(self) -> None:
        _, plan_id = await self._seed_effect("remote", finished=True)
        before = await self.store.load(owner="owner", session_id="remote")
        candidate = (await self.store.due_continuations())[0]

        result = await self._stop_while_effect_guard_held("remote")

        self.assertEqual(result["status"], "critical_pending")
        self.assertFalse(result["stopped"])
        self.assertEqual(result["stop_requested_generation"], before.generation)
        self.assertEqual(result["cancelled_continuations"], [])
        after = await self.store.load(owner="owner", session_id="remote")
        expected = deepcopy(before.metadata)
        expected["stop_requested_generation"] = before.generation
        self.assertEqual(after.metadata, expected)
        self.assertEqual(after.metadata["confirmed_publication"], before.metadata["confirmed_publication"])
        # 已存在的 pending seed 同样必须消费停止栅栏；仅保护晚到注册不足够。
        self.assertIsNone(await self.store.claim_continuation(candidate, self.binding))
        self.assertFalse(await self.runtime.session.resume_wait(candidate=candidate))
        self.assertEqual(after.metadata["effect_waits"][plan_id]["continuation"]["state"], "pending")


def test_native_completed_effect_stop_prevents_original_goal_resume(tmp_path):
    """真实模型 wire -> 签名确认 -> 原生 worker -> 冷 /stop，不能仅靠伪造 metadata。"""
    from app import database as db
    from app.repositories import organize_operation_jobs as jobs
    from tests.test_agent_effect_continuation import (
        OWNER,
        SID,
        FollowScript,
        _ready_case,
        _session,
    )

    async def run():
        async with _ready_case(tmp_path) as (_store, candidate, client, reads, stack, _users):
            cold = SQLiteKernelStore()
            follow = FollowScript(resume=True)
            resumed = _session(cold, follow, client, reads, stack=stack)
            assert await resumed.coordinator.describe(owner=OWNER, session_id=SID) is None
            before = await cold.load(owner=OWNER, session_id=SID)
            result = await stop_agent_session(SimpleNamespace(store=cold, session=resumed), OWNER, SID)
            assert result["status"] == "stopped" and result["stopped"] is True
            assert result["cancelled_continuations"] == [candidate["wait_id"]]
            assert result["background_tasks"] == []  # 已完成不是被 /stop 终止。
            assert await cold.due_continuations() == []
            assert await resumed.resume_wait(candidate=candidate) is False
            after = await cold.load(owner=OWNER, session_id=SID)
            assert after.conversation == before.conversation
            assert after.metadata["effect_waits"][candidate["wait_id"]]["final_result"] == before.metadata["effect_waits"][candidate["wait_id"]]["final_result"]
            assert follow.requests == [] and follow.model_requests == [] and reads == []
            with db.get_conn() as conn:
                operations = conn.execute("SELECT * FROM organize_operation_jobs").fetchall()
            assert len(operations) == 1
            get_organize_operation_job_result = jobs.get_organize_operation_job(operations[0]["job_id"])
            assert get_organize_operation_job_result is not None
            assert get_organize_operation_job_result["status"] == "completed"
    asyncio.run(run())


def test_native_stop_revokes_delivery_approval_then_tg_sends_buttonless_facts(tmp_path):
    """终态的 delivery 不再规划；取消新票据后由真实发送层去掉过期按钮。"""
    from app import database as db
    from app.bot import agent_adapter as adapter
    from tests import test_agent_effect_continuation as cases
    from tests.test_agent_continuation_telegram import OWNER, ROUTE, NativeBotFixture

    async def run():
        values = {"AGENT_ENABLED": "1", "TG_AGENT_ENABLED": "1", "TG_CHAT_ID": "-100", "TG_AGENT_ALLOWED_USER_IDS": "7"}
        with patch.object(cases, "OWNER", OWNER):
            async with cases._ready_case(tmp_path, channel="telegram", route=ROUTE, runtime_settings=values) as (store, candidate, client, reads, stack, _users):
                session = cases._session(store, cases.NextWriteScript(), client, reads, stack=stack)
                assert await session.resume_wait(candidate=candidate)
                cold = SQLiteKernelStore()
                delivery = (await cold.due_continuations())[0]
                assert delivery["kind"] == "delivery"
                before = await cold.load(owner=OWNER, session_id=cases.SID)
                approval_id = delivery["delivery_view"]["approval"]["plan_id"]
                assert approval_id == before.pending_effect_plan_id and approval_id != candidate["wait_id"]
                follow = cases.FollowScript(resume=True)
                runtime = SimpleNamespace(store=cold, session=cases._session(cold, follow, client, reads, stack=stack))
                # 共享 fixture 的默认票据仓库在内存；显式复用同一原生签名仓库，不伪造 revoke。
                runtime.session.pipeline.effect_store = session.pipeline.effect_store
                assert (await runtime.session.pipeline.get_pending_plan(owner=OWNER, session_id=cases.SID)).plan_id == approval_id

                result = await stop_agent_session(runtime, OWNER, cases.SID)

                assert result["status"] == "stopped" and result["confirmation"] == "revoked"
                assert result["cancelled_continuations"] == []
                after = await cold.load(owner=OWNER, session_id=cases.SID)
                assert after.pending_effect_plan_id == ""
                assert after.metadata["effect_waits"][candidate["wait_id"]] == before.metadata["effect_waits"][candidate["wait_id"]]
                assert (await cold.due_continuations())[0]["delivery_view"] == delivery["delivery_view"]
                payload = adapter.prepare_agent_continuation_delivery(
                    delivery["delivery_view"], owner=OWNER, session_id=cases.SID,
                    delivery_route=ROUTE, delivery_id=delivery["delivery_id"],
                    lifecycle_id=after.lifecycle_id,
                )
                bot = NativeBotFixture()
                with patch.object(adapter, "get_agent_kernel_runtime", return_value=runtime), patch.object(
                    adapter, "is_agent_enabled", return_value=True,
                ), patch("app.notifier.get_bot", return_value=bot):
                    sent = await asyncio.to_thread(adapter._send_agent_continuation_part, {
                        **payload, **payload["parts"][-1],
                    })
                assert sent.ok
                assert bot.sent[-1][2]["reply_markup"] is None
                assert bot.sent[-1][2]["message_thread_id"] == ROUTE["message_thread_id"]
                assert "仅为事实记录，没有执行新操作" in bot.sent[-1][1]
                assert follow.requests == [] and follow.model_requests == []
                with db.get_conn() as conn:
                    assert conn.execute("SELECT COUNT(*) FROM organize_operation_jobs").fetchone()[0] == 1
    asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
