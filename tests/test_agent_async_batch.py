"""异步组合离线回归：真实 Kernel/SQLite/队列，脚本模型与受控 Provider/poll。"""
from __future__ import annotations

import asyncio
import json
import socket
from copy import deepcopy
from dataclasses import replace
from functools import partial
from unittest.mock import Mock, patch

import httpx
import pytest

import app.agent.kernel.completion_receipts as completion_receipts
from app import database as db
from app.agent import effect_completion as effects
from app.agent import rate_limit as rates
from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.feature_gate import initialize_runtime_fence
from app.agent.kernel import batch_effects, sandbox
from app.agent.kernel.bootstrap import _capture_creation_binding
from app.agent.kernel.capabilities import (
    KernelToolSpec,
    ToolCatalog,
    ToolEffect,
)
from app.agent.kernel.continuation import CLAIM_SECONDS
from app.agent.kernel.effects import ConfirmationEffectPlanStore
from app.agent.kernel.events import AgentEventType as Event
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.ports import sdk_effects
from app.agent.kernel.session import AgentSession
from app.agent.kernel.state_types import AgentInput, CancellationToken
from app.agent.kernel.tool_calls import PreparedEffect, ToolCallContext
from app.agent.models import ToolResult
from app.modules import organize_tasks
from app.repositories import organize_operation_jobs as jobs
from tests import test_agent_session_result as session_result_tests
from tests.support import isolated_test_database
from tests.test_agent_batch_confirmation import batch_quota as shared_batch_quota
from tests.test_agent_batch_confirmation import capture, register_sdk_fixture
from tests.test_agent_effect_continuation import (
    _offline_runtime,
    _OfflineClient,
    _paused_manager,
    _run_real_worker,
)
from tests.test_agent_task_first_slice import NativeScript

batch_quota = shared_batch_quota
native_workspace = session_result_tests.native_workspace
OWNER, SID = "async-batch-owner", "async-batch-session"
FIRST, TAIL = "cloud.change", "test.preference"
GOAL = "组合变更：把 source 视频改名为 admitted.mp4，成功后保存偏好 4K，只确认一次。"


class BatchScript(NativeScript):
    def __init__(self, arguments=None, *, foreground=False):
        super().__init__("tmdb")
        self.arguments = arguments
        self.steps = ["batch"] if arguments is not None else ["finish"]
        if foreground:
            self.steps.append("finish")

    async def respond(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        step = self.steps.pop(0)
        if step == "finish":
            message = {"content": "请以本次组合的真实回执为准。"}
        else:
            name, args = "agent__run_code", {"code": f"from mf import session\nsession.batch(**{self.arguments!r})"}
            message = {"tool_calls": [{"id": "async-" + step, "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)}}]}
        return httpx.Response(200, json={"choices": [{"message": message,
            "finish_reason": "stop" if step == "finish" else "tool_calls"}]})


class AsyncBatchRig:
    def __init__(self, client, stack):
        self.client, self.stack = client, stack
        self.monkeypatch = None
        self.store = SQLiteKernelStore()
        self.worker: organize_tasks.OrganizeTaskManager | None = None
        self.value, self.attempts, self.verified = "before", [], []
        self.rate_limiter, self.denied = None, set()
        self.tail_effect = ToolEffect.WRITE
        self.client.rename = Mock(wraps=self.client.rename)
        tracked = sdk_effects.cloud_change_tool()
        self.prepare = Mock(wraps=tracked.prepare)
        self.submit = Mock(wraps=tracked.execute_confirmed)
        self.tracked = replace(tracked, prepare=self.prepare, execute_confirmed=self.submit)

    def tail(self):
        def snapshot(arguments):
            return json.dumps([self.value, arguments], sort_keys=True)

        def prepare(arguments, _context):
            return PreparedEffect(arguments=dict(arguments), snapshot_fingerprint=snapshot(arguments),
                preview=ToolResult(True, "preview", "保存偏好 4K").to_dict())

        async def execute(arguments, expected, context):
            self.attempts.append(context.active_effect_plan.plan_id)
            state = await SQLiteKernelStore().load(owner=OWNER, session_id=SID)
            progress = state.metadata["effect_waits"][self.plan.plan_id]["batch_progress"]
            assert progress["executing"] == 1 and len(progress["results"]) == 1
            assert self.plan is not None
            assert state.metadata["confirmed_publication"]["plan_id"] == self.plan.plan_id
            assert context.completion_scope is None
            assert self.client.file_info("rename").name == "admitted.mp4"
            assert expected == snapshot(arguments)
            self.value = arguments["quality"]
            return ToolResult(True, "completed", "已保存偏好 4K", data={"quality": self.value})

        def verify(arguments, result, _context):
            self.verified.append(arguments["quality"])
            assert self.value == arguments["quality"]
            return result

        return KernelToolSpec(name=TAIL, domain="test", description="保存偏好 4K",
            effect=self.tail_effect, metadata={"batch_mode": "sync", "risk": self.tail_effect.value},
            input_schema={"type": "object", "required": ["quality"], "additionalProperties": False,
                          "properties": {"quality": {"type": "string"}}},
            prepare=prepare, execute_confirmed=execute, verify=verify,
            authorize=lambda _context: TAIL not in self.denied)

    def session(self, script, store=None, *, extra=()):
        store = self.store if store is None else store
        catalog = ToolCatalog([self.tracked, self.tail(), *extra])
        register_sdk_fixture(self.monkeypatch, [self.tail(), *extra])
        pipeline = ToolPipeline(catalog=catalog, state_store=store,
            rate_limiter=self.rate_limiter,
            effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore()))
        catalog.register(sandbox.run_code_tool(pipeline))
        session = AgentSession(model=script.adapter, catalog=catalog, model_tools=("agent.run_code",),
                               pipeline=pipeline, state_store=store, journal=store)
        session.continuation_binding = _capture_creation_binding()
        return session

    async def start(self, *, foreground=False, invalid="", third=None, operation="rename"):
        arguments = {"operations": [{"op": operation, "file_id": "rename", **({"new_name": "admitted.mp4"} if operation == "rename" else {})}], "trigger_strm": False}
        steps = [{"method": FIRST, "arguments": arguments}, {"method": TAIL, "arguments": {"quality": "4K"}}]
        extra = (third,) if third is not None else ()
        if third is not None:
            steps.append({"method": third.name, "arguments": {}})
        if invalid == "trailing":
            steps.reverse()
        elif invalid == "multiple":
            extra = (replace(self.tracked, name="test.other_tracked", model_name=""),)
            steps.insert(1, {"method": extra[0].name, "arguments": arguments})
        self.script = BatchScript({"steps": steps}, foreground=foreground or bool(invalid))
        self.source = self.session(self.script, extra=extra)
        goal = GOAL + ("另将影片加入收藏。" if third is not None else "")
        view, events = await capture(self.source.run(AgentInput(message=goal, owner=OWNER, session_id=SID, channel="web")))
        if invalid:
            assert not self.script.steps
            return view, events
        assert view.approval is not None
        assert view.status == "approval_required" and view.approval.tool_name == "session.batch"
        self.plan = await self.source.pipeline.get_pending_plan(owner=OWNER, session_id=SID)
        assert self.plan is not None
        assert view.approval is not None
        assert self.plan.plan_id == view.approval.plan_id
        assert self.prepare.call_count == 1 and not self.submit.called and not self.attempts
        assert not self.job_rows()
        return view, events

    def job_rows(self):
        with db.get_conn() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM organize_operation_jobs")]

    def complete_job(self):
        row, = self.job_rows()
        assert row["status"] == "pending"
        self.worker = _run_real_worker(jobs.claim_organize_operation_job(row["job_id"]))
        get_organize_operation_job_result = jobs.get_organize_operation_job(row["job_id"])
        assert get_organize_operation_job_result is not None
        assert get_organize_operation_job_result["status"] == "completed"

    async def confirm(self, *, foreground=False):
        original_wait = effects.wait_for_effect_completion
        async def finish_before_wait(result, **options):
            self.complete_job()
            with patch.object(organize_tasks, "get_organize_manager", lambda: self.worker):
                return await original_wait(result, timeout_seconds=0, **options)
        wait = finish_before_wait if foreground else partial(original_wait, timeout_seconds=0)
        with patch.object(effects, "wait_for_effect_completion", wait):
            assert self.plan is not None
            return await capture(self.source.confirm(owner=OWNER, session_id=SID, plan_id=self.plan.plan_id, channel="web"))

    async def row(self, store=None):
        state = await (self.store if store is None else store).load(owner=OWNER, session_id=SID)
        return state, state.metadata["effect_waits"][self.plan.plan_id]

    async def poll(self, store=None):
        store = self.store if store is None else store
        def due(state):
            assert self.plan is not None
            row = (state.metadata.get("effect_waits") or {}).get(self.plan.plan_id)
            if row is not None:
                row["next_poll_at"] = 0
            state.metadata["effect_next_poll_at"] = 0
        await store.update_effect_state(owner=OWNER, session_id=SID, change=due)
        with patch.object(organize_tasks, "get_organize_manager", lambda: self.worker):
            return await effects.poll_effect_receipts(store)

    async def ready(self):
        await self.start()
        view, events = await self.confirm()
        assert view.status == "waiting" and view.approval is None
        assert not any(e.type is Event.EFFECT_APPROVAL_REQUIRED for e in events)
        self.complete_job()
        assert await self.poll() == 1
        candidate, = await SQLiteKernelStore().due_continuations()
        assert not self.attempts and self.value == "before", "poller 不得执行后项"
        return candidate

    async def assert_once(self, *, tail=True):
        assert self.prepare.call_count == self.submit.call_count == self.client.rename.call_count == 1
        assert len(self.job_rows()) == 1
        if tail:
            assert self.plan is not None
        assert self.attempts == ([self.plan.plan_id + "_2"] if tail and self.plan is not None else [])
        assert self.verified == (["4K"] if tail else [])
        events = await SQLiteKernelStore().list_events(owner=OWNER, session_id=SID)
        assert sum(e["type"] == Event.EFFECT_APPROVAL_REQUIRED.value for e in events) == 1
        assert (await self.store.load(owner=OWNER, session_id=SID)).pending_effect_plan_id == ""


@pytest.fixture
def rig(tmp_path, monkeypatch, native_workspace):
    client = _OfflineClient()
    connect = socket.socket.connect
    with isolated_test_database(), _offline_runtime(tmp_path, client) as stack, _paused_manager():
        initialize_runtime_fence()
        def local_connect(sock, address):
            assert sock.family == socket.AF_UNIX, "离线工作区仅允许Unix IPC"
            return connect(sock, address)
        stack.enter_context(patch.object(socket.socket, "connect", local_connect))
        stack.enter_context(patch.object(sdk_effects, "GuangYaClient", lambda: client))
        rig = AsyncBatchRig(client, stack)
        rig.monkeypatch = monkeypatch
        yield rig


@pytest.mark.parametrize("foreground", [False, True])
def test_tracked_and_tail_use_last_confirmed_permit_once_across_resume(rig, batch_quota, foreground):
    rig.rate_limiter, debits = batch_quota
    for name in (FIRST, TAIL):
        _, limit, cost = rates.tool_rate_limit_policy(name)
        assert cost == 1
        for _ in range(limit - 1):
            assert rates.consume_agent_tool(OWNER, name, scope_suffix="confirmed").allowed

    async def run():
        await rig.start(foreground=foreground)
        final, _ = await rig.confirm(foreground=foreground)
        if foreground:
            assert final.status == "success"
        else:
            assert final.status == "waiting"
            assert [name for name, stage, _ in debits if stage] == ["session.batch", FIRST]
            rig.complete_job()
            assert await rig.poll() == 1
            candidate, = await rig.store.due_continuations()
            cold = rig.session(BatchScript(), SQLiteKernelStore())
            assert await cold.resume_wait(candidate=candidate)
            assert not await cold.resume_wait(candidate=candidate)
        _, row = await rig.row()
        assert row["batch_progress"]["done"] is True
        assert row["final_result"]["data"]["completed"] == 2
        confirmed = [(name, decision) for name, stage, decision in debits if stage == "confirmed"]
        expected = ["session.batch", FIRST, *([] if foreground else ["session.batch"]), TAIL]
        assert [name for name, _ in confirmed] == expected
        assert all(d.allowed and d.remaining == 0 for name, d in confirmed if name in (FIRST, TAIL))
        before_replay = list(debits)
        replay, _ = await capture(rig.source.confirm(owner=OWNER, session_id=SID, plan_id=rig.plan.plan_id))
        assert replay.status == "failed" and debits == before_replay
        assert await rig.poll() == 0
        await rig.assert_once()

    asyncio.run(run())


@pytest.mark.parametrize("change", ["snapshot", "authorization"])
def test_tail_preflight_failure_never_submits_tracked_first_or_consumes_child_permits(rig, batch_quota, change):
    rig.rate_limiter, debits = batch_quota

    async def run():
        await rig.start()
        if change == "snapshot":
            rig.value = "external-change"
        else:
            rig.denied.add(TAIL)
        _, events = await rig.confirm()
        code = "confirmation_stale" if change == "snapshot" else "authorization_denied"
        assert any(e.type is Event.EFFECT_FAILED and e.payload.get("code") == code for e in events)
        assert not rig.submit.called and not rig.client.rename.called and not rig.job_rows() and not rig.attempts
        assert [name for name, stage, _ in debits if stage] == ["session.batch"]
        before_replay = list(debits)
        replay, _ = await capture(rig.source.confirm(owner=OWNER, session_id=SID, plan_id=rig.plan.plan_id))
        assert replay.status == "failed" and debits == before_replay

    asyncio.run(run())


@pytest.mark.parametrize("risk,legacy", [(ToolEffect.WRITE, False), (ToolEffect.DANGER, False), (ToolEffect.WRITE, True)])
def test_pending_poll_cold_resume_preserves_frozen_parent_and_never_replays(rig, risk, legacy):
    async def run():
        rig.tail_effect = risk
        preview, events = await rig.start()
        assert preview.approval.effect == preview.approval.confirmation["risk"] == risk.value
        assert rig.plan.effect is risk
        assert sum(e.type is Event.EFFECT_APPROVAL_REQUIRED for e in events) == 1
        waiting, _ = await rig.confirm()
        assert waiting.status == "waiting" and waiting.approval is None
        row, = rig.job_rows()
        assert row["status"] == "pending" and rig.submit.call_count == 1
        assert not rig.client.rename.called and not rig.attempts
        state, wait = await rig.row()
        seed = json.loads(completion_receipts._completion_cipher().decrypt(wait["sealed"].encode()))
        scope = rig.submit.call_args.args[2].completion_scope
        assert scope.plan_id == seed["plan_id"] == rig.plan.plan_id
        assert seed["tool"] == "session.batch" and seed["origin"] == scope.origin
        assert seed["origin"]["arguments"] == rig.plan.arguments
        assert seed["origin"]["batch_plan"]["snapshot"] == rig.plan.snapshot_fingerprint
        assert seed["origin"]["batch_plan"]["effect"] == risk.value
        assert "batch_progress" not in wait and await rig.store.due_continuations() == []
        if legacy:
            # 迁移前已签名续行：旧父标识，descriptor没有tool；原参数/任务/检查点不变。
            seed["tool"] = "agent.batch"
            del seed["origin"]["batch_plan"]["tool"]
            sealed = completion_receipts._completion_cipher().encrypt(json.dumps(seed).encode()).decode()
            await rig.store.update_effect_state(owner=OWNER, session_id=SID,
                change=lambda state: state.metadata["effect_waits"][rig.plan.plan_id].update(sealed=sealed))
        users = [r for r in state.conversation if r["role"] == "user"]
        rig.complete_job()
        cold = SQLiteKernelStore()
        assert await rig.poll(cold) == 1
        _, wait = await rig.row(cold)
        progress = wait["batch_progress"]
        first, = progress["results"]
        assert first["tool"] == FIRST and first["result"]["data"]["background_job"]["status"] == "completed"
        assert progress["executing"] is None and progress["done"] is False
        assert not rig.attempts, "只读 poll 不得执行同步后项"
        candidate, = await cold.due_continuations()
        script = BatchScript()
        resumed = rig.session(script, cold)
        assert resumed is not rig.source and resumed.pipeline is not rig.source.pipeline
        assert await resumed.resume_wait(candidate=candidate)
        final, wait = await rig.row(cold)
        progress = wait["batch_progress"]
        assert progress["executing"] is None and progress["done"] is True
        assert len(progress["results"]) == 2 and progress["results"][0] == first
        assert progress["results"][1]["tool"] == TAIL
        assert progress["results"][1]["result"]["status"] == "completed"
        assert wait["final_result"]["data"]["completed"] == wait["final_result"]["data"]["total"] == 2
        assert [r for r in final.conversation if r["role"] == "user"] == users
        assert len(script.requests) == 1 and not script.steps
        receipts = [json.loads(r["content"]) for r in script.requests[0]["messages"]
                    if r["role"] == "tool" and r.get("tool_call_id") == "async-batch"]
        assert len(receipts) == 1 and receipts[0]["data"]["completed"] == 2
        for _ in range(2):
            assert await rig.poll(cold) == 0
            assert not await rig.session(BatchScript(), SQLiteKernelStore()).resume_wait(candidate=candidate)
        assert await SQLiteKernelStore().due_continuations() == []
        replay, _ = await capture(resumed.confirm(owner=OWNER, session_id=SID, plan_id=rig.plan.plan_id))
        assert replay.status == "failed"
        await rig.assert_once()
    asyncio.run(run())


def test_foreground_fast_completion_executes_each_step_once_without_background_replay(rig):
    async def run():
        await rig.start(foreground=True)
        final, events = await rig.confirm(foreground=True)
        assert final.status == "success" and final.approval is None
        assert final.effect_result["data"]["completed"] == 2
        assert not any(e.type is Event.EFFECT_APPROVAL_REQUIRED for e in events)
        assert await SQLiteKernelStore().due_continuations() == []
        assert not rig.script.steps
        _, wait = await rig.row()
        assert wait["batch_progress"]["done"] is True
        cold = SQLiteKernelStore()
        assert await rig.poll(cold) == 0
        assert not await rig.session(BatchScript(), cold).resume_wait(candidate=wait["continuation"])
        await rig.assert_once()
    asyncio.run(run())


@pytest.mark.parametrize("interruption", ["stop", "new_user"])
@pytest.mark.parametrize("phase", ["pending", "ready"])
def test_stop_or_new_user_never_executes_old_remaining_write(rig, interruption, phase):
    async def run():
        candidate = None
        if phase == "ready":
            candidate = await rig.ready()
        else:
            await rig.start()
            waiting, _ = await rig.confirm()
            assert waiting.status == "waiting"
        state, _ = await rig.row()
        if interruption == "stop":
            assert await rig.store.request_stop_generation(owner=OWNER, session_id=SID, generation=state.generation)
        else:
            fresh = rig.session(BatchScript())
            view, _ = await capture(fresh.run(AgentInput(message="取消旧目标，只答复这条新消息。", owner=OWNER, session_id=SID, channel="web")))
            assert view.status == "success"
        if phase == "pending":
            rig.complete_job()
            assert await rig.poll() == 1
            candidate, = await rig.store.due_continuations()
        cold, script = SQLiteKernelStore(), BatchScript()
        assert candidate is not None
        assert not await rig.session(script, cold).resume_wait(candidate=candidate)
        assert not script.requests
        assert await cold.due_continuations() == []
        assert rig.client.file_info("rename").name == "admitted.mp4" and rig.value == "before"
        await rig.assert_once(tail=False)
    asyncio.run(run())


@pytest.mark.parametrize("stop", [False, True])
def test_durable_executing_marker_is_unknown_after_restart_and_never_replayed(rig, stop):
    async def run():
        candidate = await rig.ready()
        claimed = await rig.store.claim_continuation(candidate, _capture_creation_binding())
        assert claimed is not None, "原确认未失效时必须可领取写前检查点租约"
        lease, _ = claimed
        async def progress(_event):
            pass
        context = ToolCallContext(owner=OWNER, session_id=SID, request_id=lease.request_id, turn_id=lease.turn_id,
            lease=lease, cancellation=CancellationToken(), report_progress=progress, active_effect_plan=rig.plan)
        # 真正写前检查点已提交；模拟进程在写入回执前死亡，绝不能据此推断写入未发生。
        assert await batch_effects._checkpoint(rig.source.pipeline, context, 1)
        _, marked = await rig.row()
        first, = marked["batch_progress"]["results"]
        assert marked["batch_progress"]["executing"] == 1
        if stop:
            assert await rig.store.request_stop_generation(owner=OWNER, session_id=SID, generation=lease.generation)
        cold = SQLiteKernelStore(clock=lambda: rig.store._clock() + CLAIM_SECONDS + 1)
        retry, = await cold.due_continuations()
        script = BatchScript()
        assert await rig.session(script, cold).resume_wait(candidate=retry) is (not stop)
        _, recovered = await rig.row(cold)
        progress = recovered["batch_progress"]
        assert progress["results"][0] == first
        if not stop:
            assert progress["done"] is True and progress["executing"] is None
            assert progress["results"][1]["result"]["status"] == "outcome_unknown"
            assert recovered["final_result"]["data"]["completed"] == 1
        else:
            assert progress == marked["batch_progress"] and not script.requests
            assert recovered["continuation"]["state"] == "cancelled"
        assert not await rig.session(BatchScript(), cold).resume_wait(candidate=retry)
        assert await cold.due_continuations() == [] and rig.value == "before"
        await rig.assert_once(tail=False)
    asyncio.run(run())


def test_late_tail_snapshot_drift_keeps_first_terminal_receipt_and_refuses_tail(rig):
    async def run():
        candidate = await rig.ready()
        _, ready = await rig.row()
        first, = ready["batch_progress"]["results"]
        rig.value = "external-change"
        cold = SQLiteKernelStore()
        assert await rig.session(BatchScript(), cold).resume_wait(candidate=candidate)
        _, final = await rig.row(cold)
        progress = final["batch_progress"]
        assert progress["results"][0] == first
        assert progress["results"][1]["result"]["status"] == "not_started"
        assert progress["done"] is True and progress["executing"] is None
        assert final["final_result"]["data"]["completed"] == 1
        assert rig.value == "external-change" and rig.client.file_info("rename").name == "admitted.mp4"
        assert not await rig.session(BatchScript(), cold).resume_wait(candidate=candidate)
        await rig.assert_once(tail=False)
    asyncio.run(run())


@pytest.mark.parametrize("invalid", ["trailing", "multiple"])
def test_tracked_must_be_unique_and_first_before_any_child_prepares(rig, invalid):
    async def run():
        view, events = await rig.start(invalid=invalid)
        assert view.approval is None
        assert "invalid_arguments" in json.dumps([dict(e.payload) for e in events], ensure_ascii=False)
        assert not rig.prepare.called and not rig.submit.called and not rig.attempts and not rig.job_rows()
    asyncio.run(run())


def test_old_poll_rebuilds_from_new_checkpoints_after_three_real_kernel_steps(rig):
    async def run():
        third_calls, verified, stale = [], [], []
        async def third_execute(_args, expected, context):
            state, row = await rig.row(SQLiteKernelStore())
            assert row["batch_progress"]["executing"] == 2
            assert len(row["batch_progress"]["results"]) == 2 and expected == "favorite-before"
            assert state.metadata["confirmed_publication"]["plan_id"] == rig.plan.plan_id
            third_calls.append(context.active_effect_plan.plan_id)
            return ToolResult(True, "completed", "已加入收藏", data={"favorite": True})
        def third_verify(_args, result, _context):
            verified.append(result.data["favorite"])
            return result
        third = KernelToolSpec(name="test.favorite", domain="test", description="加入收藏",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            effect=ToolEffect.WRITE, metadata={"batch_mode": "sync"},
            prepare=lambda _a, _c: PreparedEffect(snapshot_fingerprint="favorite-before",
                preview=ToolResult(True, "preview", "加入收藏").to_dict()),
            execute_confirmed=third_execute, verify=third_verify)
        await rig.start(third=third)
        waiting, _ = await rig.confirm()
        assert waiting.status == "waiting" and len(rig.plan.arguments["steps"]) == 3
        rig.complete_job()
        assert await rig.poll() == 1
        candidate, = await rig.store.due_continuations()
        cold, script = SQLiteKernelStore(), BatchScript()
        def retry_delivery(state):
            state.metadata["effect_waits"][rig.plan.plan_id]["delivered"] = False
        await cold.update_effect_state(owner=OWNER, session_id=SID, change=retry_delivery)
        original_update, original_resume = cold.update_effect_state, batch_effects.resume_batch
        async def fail_before_receipt(*args, **kwargs):
            result = await original_resume(*args, **kwargs)
            assert result.outcome.public_content["data"]["completed"] == 3
            raise RuntimeError("注入：全部检查点已提交，尚未保存最终回执模板")
        async def interleave(**options):
            value = await original_update(**options)
            if options["change"].__name__ == "claim" and value is not None:
                stale.append(deepcopy(value["final_result"]))
                with patch.object(batch_effects, "resume_batch", fail_before_receipt):
                    assert await rig.session(script, SQLiteKernelStore(), extra=(third,)).resume_wait(candidate=candidate)
                _, advanced = await rig.row(cold)
                assert advanced["batch_progress"]["done"] is True
                assert not advanced.get("receipt_template") and not script.requests
            return value
        # 旧 poll 已领取缓存的 1/3 running，受保护执行器随后提交 3/3，且未留下最终模板。
        with patch.object(cold, "update_effect_state", interleave):
            assert await rig.poll(cold) == 1
        assert len(stale) == 1 and stale[0]["status"] == "running" and stale[0]["data"]["completed"] == 1
        state, final = await rig.row(cold)
        assert final["final_result"]["status"] == "completed"
        assert final["final_result"]["data"]["completed"] == 3
        assert [item["tool"] for item in final["batch_progress"]["results"]] == [FIRST, TAIL, third.name]
        assert final["batch_progress"]["executing"] is None
        receipt = json.loads(final["receipt_message"]["content"].split("\n", 1)[1])
        assert receipt["data"]["completed"] == 3
        native, = [r for r in state.conversation if r.get("tool_call_id") == "async-batch" and r["role"] == "tool"]
        assert json.loads(native["content"])["data"]["completed"] == 3
        assert third_calls == [rig.plan.plan_id + "_3"] and verified == [True]
        assert await rig.poll(cold) == 0 and await cold.due_continuations() == []
        await rig.assert_once()
    asyncio.run(run())


@pytest.mark.parametrize("reason", ["expired", "cancelled"])
def test_retired_batch_continuation_poll_has_no_running_or_followup_promise(rig, reason):
    async def run():
        candidate = await rig.ready()
        state, row = await rig.row()
        progress = deepcopy(row["batch_progress"])
        cold = SQLiteKernelStore(clock=lambda: row["continuation"]["expires_at"] + 1) if reason == "expired" else SQLiteKernelStore()
        if reason == "cancelled":
            assert await cold.request_stop_generation(owner=OWNER, session_id=SID, generation=state.generation)
        script = BatchScript()
        assert candidate is not None
        assert not await rig.session(script, cold).resume_wait(candidate=candidate)
        _, retired = await rig.row(cold)
        assert retired["continuation"]["state"] == ("terminal" if reason == "expired" else "cancelled")
        assert retired["continuation"]["terminal_reason"] == ("expired_or_runtime_changed" if reason == "expired" else "cancelled")
        def retry_delivery(state):
            state.metadata["effect_waits"][rig.plan.plan_id]["delivered"] = False
        await cold.update_effect_state(owner=OWNER, session_id=SID, change=retry_delivery)
        assert await rig.poll(cold) == 1
        _, final = await rig.row(cold)
        assert final["batch_progress"] == progress and final["final_result"]["status"] == "partial"
        assert final["final_result"]["data"]["completed"] == 1
        assert not final["final_result"]["data"].get("background_job", {}).get("followup_pending")
        assert "自动接续" not in final["receipt_message"]["public_content"]
        assert not script.requests and await cold.due_continuations() == []
        await rig.assert_once(tail=False)
    asyncio.run(run())


def test_post_write_checkpoint_failure_is_durably_unknown_and_never_replayed(rig):
    async def run():
        candidate = await rig.ready()
        _, ready = await rig.row()
        first = deepcopy(ready["batch_progress"]["results"][0])
        cold, script, injected = SQLiteKernelStore(), BatchScript(), []
        original = batch_effects._checkpoint
        async def fail_result_checkpoint(pipeline, context, index, result=None):
            if index == 1 and result is not None and not injected:
                _, marked = await rig.row(SQLiteKernelStore())
                assert marked["batch_progress"]["executing"] == index
                assert marked["batch_progress"]["results"] == [first]
                assert result["status"] == "completed" and rig.value == "4K"
                assert rig.attempts == [rig.plan.plan_id + "_2"] and rig.verified == ["4K"]
                injected.append(index)
                raise RuntimeError("注入：真实后项已写入并核验，结果 checkpoint 一次性失败")
            return await original(pipeline, context, index, result)
        with patch.object(batch_effects, "_checkpoint", fail_result_checkpoint):
            assert await rig.session(script, cold).resume_wait(candidate=candidate)
        assert injected == [1] and not script.requests
        _, final = await rig.row(SQLiteKernelStore())
        progress = final["batch_progress"]
        assert progress["done"] is True and progress["executing"] is None
        assert len(progress["results"]) == 2 and progress["results"][0] == first
        assert progress["results"][1]["tool"] == TAIL
        assert progress["results"][1]["result"]["status"] == "outcome_unknown"
        assert final["continuation"]["state"] == "terminal"
        assert final["final_result"]["status"] == "partial"
        assert final["final_result"]["data"]["completed"] == 1
        assert final["final_result"]["data"]["steps"] == progress["results"]
        receipt = json.loads(final["receipt_message"]["content"].split("\n", 1)[1])
        assert receipt["status"] == "partial" and receipt["data"]["steps"] == progress["results"]
        future = SQLiteKernelStore(clock=lambda: cold._clock() + CLAIM_SECONDS + 1)
        retry_script = BatchScript()
        for _ in range(2):
            await rig.poll(future)
            assert not await rig.session(retry_script, future).resume_wait(candidate=candidate)
            _, repeated = await rig.row(future)
            assert repeated["batch_progress"] == progress
        assert not retry_script.requests and await future.due_continuations() == []
        await rig.assert_once()
    asyncio.run(run())


def test_stop_while_unprotecting_batch_prevents_any_model_call(rig):
    async def run():
        candidate = await rig.ready()
        cold, script, injected = SQLiteKernelStore(), BatchScript(), []
        session = rig.session(script, cold)
        original = session.coordinator.unprotect
        async def stop_before_unprotect(lease, token):
            active = await session.coordinator.describe(owner=OWNER, session_id=SID)
            assert active["protected"] is True and active["generation"] == lease.generation
            assert await cold.request_stop_generation(owner=OWNER, session_id=SID, generation=lease.generation)
            assert not await session.coordinator.cancel(owner=OWNER, session_id=SID, reason="user_cancelled")
            injected.append(lease)
            await original(lease, token)
        with patch.object(session.coordinator, "unprotect", stop_before_unprotect):
            assert await session.resume_wait(candidate=candidate)
        assert len(injected) == 1
        state, final = await rig.row(SQLiteKernelStore())
        assert state.metadata["stop_requested_generation"] == injected[0].generation
        assert not script.requests, "保护期间收到 stop，解除保护后不能再调用模型"
        assert final["batch_progress"]["done"] is True and final["batch_progress"]["executing"] is None
        assert final["final_result"]["data"]["completed"] == 2
        events = await cold.list_events(owner=OWNER, session_id=SID)
        assert any(e["type"] == Event.TURN_CANCELLED.value and e["turn_id"] == injected[0].turn_id for e in events)
        assert not await session.resume_wait(candidate=candidate)
        assert await cold.due_continuations() == []
        await rig.assert_once()
    asyncio.run(run())


@pytest.mark.parametrize("operation,risk", [("rename", ToolEffect.WRITE), ("trash", ToolEffect.DANGER)])
def test_cloud_parameter_risk_reaches_parent_child_and_receipt(rig, operation, risk):
    async def run():
        preview, _ = await rig.start(operation=operation)
        assert rig.plan.effect is risk and preview.approval.effect == risk.value
        waiting, _ = await rig.confirm()
        assert waiting.status == "waiting"
        child = rig.submit.call_args.args[2].active_effect_plan
        assert child.effect is risk
        _, wait = await rig.row()
        seed = json.loads(completion_receipts._completion_cipher().decrypt(wait["sealed"].encode()))
        assert seed["origin"]["batch_plan"]["effect"] == risk.value
        assert not rig.attempts and len(rig.job_rows()) == 1
    asyncio.run(run())
