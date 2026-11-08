"""F16：真实 Kernel/SQLite 确认链路，领域写入仅使用内存 handler。"""

import asyncio
import json
from dataclasses import replace
from inspect import Parameter, Signature
from types import SimpleNamespace

import pytest

from app import sdk
from app.agent import rate_limit as rates
from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.errors import ConfirmationClaimError, ToolPipelineError
from app.agent.kernel import sandbox
from app.agent.kernel.adapters import TurnViewBuilder
from app.agent.kernel.capabilities import (
    KernelToolSpec,
    ToolCatalog,
    ToolEffect,
)
from app.agent.kernel.effects import (
    ConfirmationEffectPlanStore,
    EffectExecutor,
    InMemoryRateLimiter,
)
from app.agent.kernel.events import AgentEventType as Event
from app.agent.kernel.model import ModelEvent, ModelEventType, ModelToolCall
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.ports import mediaflux_policy
from app.agent.kernel.session import AgentSession
from app.agent.kernel.state_types import AgentInput, CancellationToken
from app.agent.kernel.tool_calls import PreparedEffect, ToolCallContext
from app.agent.models import ToolResult
from tests import test_agent_session_result as session_result_tests
from tests.test_agent_kernel_core import ScriptedModel, collect
from tests.test_agent_task_cas import task_store as isolated_store

task_store = isolated_store
native_workspace = session_result_tests.native_workspace

OWNER, SESSION = "batch-regression-owner", "batch-regression-session"
NAMES = ("test.watchlist", "test.preferences", "test.third", "test.fourth")
BACKGROUND = "test.background"
FIELDS = dict(zip((*NAMES, BACKGROUND), ("item", "quality", "label", "tag", "path")))
FINAL = [ModelEvent(ModelEventType.TEXT_DELTA, text="请按系统回执核对结果。"),
         ModelEvent(ModelEventType.FINISH, finish_reason="stop")]


def register_sdk_fixture(monkeypatch, tools):
    """测试端口声明真实Python签名；工作区与批次仍走生产RPC/签名校验。"""
    for tool in tools:
        if tool.name in sdk.METHODS:
            continue
        def method(**kwargs):
            raise AssertionError("写入必须由EffectExecutor执行")
        properties = tool.input_schema.get("properties", {})
        required = tool.input_schema.get("required", [])
        monkeypatch.setattr(method, "__signature__", Signature([Parameter(key, Parameter.KEYWORD_ONLY, annotation=str,
            default=Parameter.empty if key in required else value.get("default", None)) for key, value in properties.items()]), raising=False)
        monkeypatch.setitem(sdk.METHODS, tool.name, method)
        namespace = tool.name.split(".")[0]
        if not hasattr(sdk, namespace):
            monkeypatch.setattr(sdk, namespace, SimpleNamespace(), raising=False)
    monkeypatch.setattr(sdk, "WRITE_METHODS", sdk.WRITE_METHODS | {tool.name for tool in tools})
    monkeypatch.setattr(sandbox, "METHOD_EFFECTS", {**sdk.METHOD_EFFECTS, **{tool.name: "write" for tool in tools}})


def step(name):
    return {"method": name, "arguments": {FIELDS.get(name, "item"): f" {name} "}}


def batch(*names):
    return {"steps": [step(name) for name in names]}


def call(name, arguments):
    if name == "session.batch" or name in FIELDS:
        name, arguments = "agent.run_code", {"code": f"import mf\nmf.{name}(**{arguments!r})"}
    return [ModelEvent(ModelEventType.TOOL_CALL_COMPLETED,
                       tool_call=ModelToolCall("regression-call", name, arguments)),
            ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")]


async def capture(stream):
    events = await collect(stream)
    builder = TurnViewBuilder()
    for event in events:
        builder.apply(event)
    return builder.build(), events


async def confirm(session, plan_id):
    return await capture(session.confirm(owner=OWNER, session_id=SESSION, plan_id=plan_id))


class BatchRig:
    def __init__(self, store):
        self.store = store
        self.values = dict.fromkeys(FIELDS, "before")
        self.writes, self.attempted, self.verified, self.trace = [], [], [], []
        self.fail_at, self.fail_verify, self.denied = set(), set(), set()
        self.rate_limiter = None

    def tool(self, name):
        field = FIELDS[name]

        def normalized(arguments):
            return {field: arguments[field].strip()}

        def snapshot(arguments):
            return json.dumps([name, self.values[name], normalized(arguments)], sort_keys=True)

        def prepare(arguments, _context):
            self.trace.append(("prepare", name))
            return PreparedEffect(
                preview=ToolResult(True, "preview", f"将写入 {name}",
                                   data={"target": normalized(arguments)}).to_dict(),
                arguments=normalized(arguments), snapshot_fingerprint=snapshot(arguments))

        def execute(arguments, expected, _context):
            self.attempted.append(name)
            self.trace.append(("execute", name))
            if expected != snapshot(arguments):
                raise ToolPipelineError("领域快照已变化", code="confirmation_stale")
            if name in self.fail_at:
                raise ToolPipelineError("注入领域失败", code="fixture_failure")
            self.values[name] = arguments[field]
            self.writes.append((name, dict(arguments)))
            return ToolResult(True, "completed", f"已写入 {name}",
                              data={"receipt": f"receipt:{name}", "value": self.values[name]})

        def verify(arguments, result, _context):
            self.verified.append(name)
            self.trace.append(("verify", name))
            assert self.values[name] == arguments[field]
            return False if name in self.fail_verify else result

        return KernelToolSpec(
            name=name, domain="test", description=f"同步组合回归操作 {name}",
            effect=ToolEffect.WRITE,
            input_schema={"type": "object", "required": [field], "additionalProperties": False,
                          "properties": {field: {"type": "string", "minLength": 1}}},
            prepare=prepare, execute_confirmed=execute, verify=verify,
            authorize=lambda _context: name not in self.denied,
            metadata={"batch_mode": "sync" if name != BACKGROUND else ""})

    def session(self, rounds, store=None):
        store = store if store is not None else self.store
        catalog = ToolCatalog([self.tool(name) for name in FIELDS])
        pipeline = ToolPipeline(catalog=catalog, state_store=store,
                                rate_limiter=self.rate_limiter,
                                effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore()))
        catalog.register(sandbox.run_code_tool(pipeline))
        return AgentSession(model=ScriptedModel(rounds), catalog=catalog,
                            model_tools=("agent.run_code",), pipeline=pipeline, state_store=store)

    async def start(self, arguments=None, *, tool="session.batch", followups=None):
        arguments = batch(*NAMES[:2]) if arguments is None else arguments
        session = self.session([call(tool, arguments), *(followups or [FINAL])])
        view, events = await capture(session.run(AgentInput(
            message=f"执行 {tool} 同步组合回归操作，确认前不得写入。", owner=OWNER, session_id=SESSION)))
        return session, view, events


@pytest.fixture
def rig(task_store, monkeypatch, native_workspace):
    monkeypatch.setattr("app.modules.web_secret.get_web_secret", lambda: "batch-test-only")
    rig = BatchRig(task_store)
    register_sdk_fixture(monkeypatch, [rig.tool(name) for name in FIELDS])
    return rig


@pytest.fixture
def batch_quota(monkeypatch):
    limiter = rates.AgentRateLimiter(clock=lambda: 100.0)
    debits = []
    consume = rates.consume_agent_tool

    def recorded(owner, name, *, scope_suffix=""):
        decision = consume(owner, name, scope_suffix=scope_suffix)
        debits.append((name, scope_suffix, decision))
        return decision

    monkeypatch.setattr(rates, "agent_rate_limiter", limiter)
    monkeypatch.setattr(mediaflux_policy, "consume_agent_tool", recorded)
    return mediaflux_policy.MediaFluxToolRateLimiter(), debits


@pytest.mark.parametrize("last_permit", [False, True])
def test_each_child_uses_one_confirmed_permit_independent_of_exhausted_preview(rig, batch_quota, last_permit):
    rig.rate_limiter, debits = batch_quota
    names = ["session.batch", *NAMES[:2]]
    for name in names:
        _, limit, cost = rates.tool_rate_limit_policy(name)
        assert cost == 1
        for _ in range(limit - 1):
            assert rates.consume_agent_tool(OWNER, name).allowed
            if last_permit:
                assert rates.consume_agent_tool(OWNER, name, scope_suffix="confirmed").allowed

    async def run():
        session, preview, _ = await rig.start()
        assert preview.status == "approval_required"
        assert [(name, stage) for name, stage, _ in debits] == [("agent.run_code", ""), *[(name, "") for name in names]]
        assert all(decision.allowed and decision.remaining == 0 for _, _, decision in debits[1:])
        final, _ = await confirm(session, preview.approval.plan_id)
        assert final.status == "success" and final.effect_result["data"]["completed"] == 2
        assert [name for name, _ in rig.writes] == list(NAMES[:2])
        confirmed = [(name, decision) for name, stage, decision in debits if stage == "confirmed"]
        assert [name for name, _ in confirmed] == names
        assert all(d.allowed and d.cost == rates.tool_rate_limit_policy(name)[2] for name, d in confirmed)
        assert all(d.remaining == (0 if last_permit else d.limit - d.cost) for _, d in confirmed)
        before_replay = list(debits)
        replay, _ = await confirm(session, preview.approval.plan_id)
        assert replay.status == "failed" and debits == before_replay
        assert rig.attempted == rig.verified == list(NAMES[:2])

    asyncio.run(run())


@pytest.mark.parametrize("cost", [0.25, 2.5])
def test_weighted_child_cost_is_consumed_once_with_confirmation_minimum(rig, monkeypatch, cost):
    tool = rig.tool
    monkeypatch.setattr(rig, "tool", lambda name: replace(tool(name), cost=cost))
    rig.rate_limiter = InMemoryRateLimiter(limit=3)

    async def run():
        for name in NAMES[:2]:
            await rig.rate_limiter.acquire(owner=OWNER, tool_name=f"confirm:{name}",
                cost=3 - max(1.0, cost), arguments={})
        session, preview, _ = await rig.start()
        assert preview.status == "approval_required"
        final, _ = await confirm(session, preview.approval.plan_id)
        assert final.status == "success" and rig.attempted == list(NAMES[:2])
        for name in NAMES[:2]:
            with pytest.raises(ToolPipelineError) as rejected:
                await rig.rate_limiter.acquire(owner=OWNER, tool_name=f"confirm:{name}", cost=0.1, arguments={})
            assert rejected.value.code == "rate_limited"

    asyncio.run(run())


def test_execution_quota_rejection_keeps_first_receipt_and_never_replays(rig, batch_quota):
    rig.rate_limiter, debits = batch_quota
    _, limit, _ = rates.tool_rate_limit_policy(NAMES[1])
    for _ in range(limit):
        assert rates.consume_agent_tool(OWNER, NAMES[1], scope_suffix="confirmed").allowed

    async def run():
        session, preview, _ = await rig.start(batch(*NAMES[:3]))
        final, _ = await confirm(session, preview.approval.plan_id)
        assert final.status == "partial" and final.effect_result["data"]["completed"] == 1
        first, failed, skipped = final.effect_result["data"]["steps"]
        assert first["result"]["data"]["receipt"] == f"receipt:{NAMES[0]}"
        assert failed["result"]["error"] == "rate_limited" and skipped["result"]["status"] == "not_started"
        assert rig.attempted == rig.verified == [NAMES[0]] and len(rig.writes) == 1
        assert [(name, decision.allowed) for name, stage, decision in debits if stage] == [
            ("session.batch", True), (NAMES[0], True), (NAMES[1], False)]
        before_replay = list(debits)
        replay, _ = await confirm(session, preview.approval.plan_id)
        assert replay.status == "failed" and debits == before_replay

    asyncio.run(run())


def test_execution_rechecks_authorization_after_all_child_preflights(rig, batch_quota, monkeypatch):
    rig.rate_limiter, debits = batch_quota

    async def run():
        session, preview, _ = await rig.start()
        authorize = session.pipeline.authorization.authorize
        checked = []

        async def revoke_after_preflight(tool, arguments, context):
            checked.append(tool.name)
            await authorize(tool, arguments, context)
            if tool.name == NAMES[1]:
                rig.denied.add(NAMES[0])

        monkeypatch.setattr(session.pipeline.authorization, "authorize", revoke_after_preflight)
        final, _ = await confirm(session, preview.approval.plan_id)
        assert checked == ["session.batch", *NAMES[:2], NAMES[0]]
        assert final.effect_result["data"]["steps"][0]["result"]["error"] == "authorization_denied"
        assert not rig.writes and not rig.attempted and not rig.verified
        assert [name for name, stage, _ in debits if stage] == ["session.batch"]

    asyncio.run(run())


def test_cancellation_after_execution_permit_prevents_handler_start(rig, batch_quota, monkeypatch):
    rig.rate_limiter, debits = batch_quota

    async def run():
        pipeline = rig.session([]).pipeline
        lease, _ = await rig.store.begin_turn(owner=OWNER, session_id=SESSION, request_id="cancel-permit")

        async def progress(_event):
            pass

        context = ToolCallContext(owner=OWNER, session_id=SESSION, request_id=lease.request_id,
            turn_id=lease.turn_id, lease=lease, cancellation=CancellationToken(), report_progress=progress)
        preview = await pipeline.execute("session.batch", batch(*NAMES[:2]), context=context)
        acquire = rig.rate_limiter.acquire

        async def cancel_after_acquire(**options):
            await acquire(**options)
            if options["tool_name"] == f"confirm:{NAMES[0]}":
                context.cancellation.cancel()

        monkeypatch.setattr(rig.rate_limiter, "acquire", cancel_after_acquire)
        with pytest.raises(asyncio.CancelledError):
            await pipeline.execute_confirmed(preview.effect_plan.plan_id, context=context)
        assert rig.trace == [("prepare", name) for name in (*NAMES[:2], *NAMES[:2])]
        assert not rig.writes and not rig.attempted and not rig.verified
        assert [name for name, stage, _ in debits if stage] == ["session.batch", NAMES[0]]
        assert await pipeline.get_pending_plan(owner=OWNER, session_id=SESSION) is None

    asyncio.run(run())


def test_concurrent_confirmation_never_replays_or_debits_children_again(rig, batch_quota, monkeypatch):
    rig.rate_limiter, debits = batch_quota

    async def run():
        session, preview, _ = await rig.start()
        cold = rig.session([FINAL], SQLiteKernelStore(secret_provider=lambda: "task-test-only"))
        entered, release = asyncio.Event(), asyncio.Event()
        execute = session.pipeline._run_effect

        async def hold_first(plan, *, context):
            if plan.tool_name == NAMES[0]:
                entered.set()
                await release.wait()
            return await execute(plan, context=context)

        monkeypatch.setattr(session.pipeline, "_run_effect", hold_first)
        running = asyncio.create_task(confirm(session, preview.approval.plan_id))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            rejected, _ = await asyncio.wait_for(confirm(cold, preview.approval.plan_id), timeout=5)
            assert rejected.status != "success"
        finally:
            release.set()
            final, _ = await running
        assert final.status == "success"
        replay, _ = await confirm(cold, preview.approval.plan_id)
        assert replay.status == "failed"
        assert [name for name, stage, _ in debits if stage] == ["session.batch", *NAMES[:2]]
        assert rig.attempted == rig.verified == list(NAMES[:2]) and len(rig.writes) == 2

    asyncio.run(run())


def test_native_batch_visibility_and_supported_methods(rig):
    from app.agent.kernel.ports.sdk_effects import (
        cloud_change_tool,
        preferences_tools,
        watchlist_tools,
    )
    catalog = ToolCatalog([cloud_change_tool(), *watchlist_tools(), *preferences_tools()])
    pipeline = ToolPipeline(catalog=catalog, state_store=rig.store)
    assert {t.name for t in catalog.visible() if t.metadata.get("batch_mode")} == {"watchlist.add", "preferences.update", "cloud.change"}
    assert not catalog.has("agent.batch")
    assert not any(t.name == "session.batch" for t in catalog.visible())
    assert pipeline.catalog.has("session.batch")


def test_one_card_two_writes_and_single_use_ticket(rig):
    async def run():
        session, preview, events = await rig.start()
        assert preview.status == "approval_required" and preview.approval.tool_name == "session.batch"
        assert preview.approval.confirmation["risk"] == "write"
        assert sum(e.type is Event.EFFECT_APPROVAL_REQUIRED for e in events) == 1
        assert [s["tool"] for s in preview.approval.preview["data"]["steps"]] == list(NAMES[:2])
        assert not rig.writes and not rig.attempted and not rig.verified
        final, events = await confirm(session, preview.approval.plan_id)
        assert final.status == "success" and final.approval is None
        assert sum(e.type is Event.EFFECT_COMPLETED for e in events) == 1
        assert not any(e.type is Event.EFFECT_APPROVAL_REQUIRED for e in events)
        assert rig.writes == [(n, {FIELDS[n]: n}) for n in NAMES[:2]]
        assert rig.verified == list(NAMES[:2])
        assert rig.trace[:4] == [("prepare", n) for n in (*NAMES[:2], *NAMES[:2])]
        data = final.effect_result["data"]
        assert data["completed"] == data["total"] == 2
        assert [s["result"]["data"]["receipt"] for s in data["steps"]] == [f"receipt:{n}" for n in NAMES[:2]]
        replay, _ = await confirm(session, preview.approval.plan_id)
        assert replay.status == "failed"
        assert rig.attempted == rig.verified == list(NAMES[:2]) and len(rig.writes) == 2
    asyncio.run(run())


def test_batch_unknown_step_key_repair_reaches_one_confirmation(rig):

    async def run():
        fixed = batch(*NAMES[:2])
        invalid = {'steps': [{'method': s['method'], 'parameters': s['arguments']}
                             for s in fixed['steps']]}
        session = rig.session([call('session.batch', invalid), call('session.batch', fixed)])
        view, events = await capture(session.run(AgentInput(
            owner=OWNER, session_id=SESSION, message='session.batch 合并收藏和偏好，一次确认')))
        assert view.status == 'approval_required'
        assert "invalid_arguments" in json.dumps([dict(e.payload) for e in events], ensure_ascii=False)
        assert not rig.writes
        plan = await session.pipeline.get_pending_plan(owner=OWNER, session_id=SESSION)
        assert plan and plan.tool_name == 'session.batch'
        assert [s['tool'] for s in plan.arguments['steps']] == list(NAMES[:2])
        session.model = ScriptedModel([FINAL])
        result, _ = await confirm(session, plan.plan_id)
        assert result.status == 'success'
        assert [name for name, _ in rig.writes] == list(NAMES[:2])
    asyncio.run(run())


def test_cancelled_batch_never_writes(rig):
    async def run():
        session, preview, _ = await rig.start()
        assert await session.cancel_effect(owner=OWNER, session_id=SESSION, plan_id=preview.approval.plan_id)
        assert not await session.cancel_effect(owner=OWNER, session_id=SESSION, plan_id=preview.approval.plan_id)
        result, _ = await confirm(session, preview.approval.plan_id)
        assert result.status == "failed"
        assert not rig.writes and not rig.attempted and not rig.verified
    asyncio.run(run())


@pytest.mark.parametrize("change", ["snapshot", "authorization"])
def test_second_step_preflight_failure_means_zero_writes(rig, change, batch_quota):
    rig.rate_limiter, debits = batch_quota

    async def run():
        session, preview, _ = await rig.start()
        if change == "snapshot":
            rig.values[NAMES[1]] = "external-change"
        else:
            rig.denied.add(NAMES[1])
        _, events = await confirm(session, preview.approval.plan_id)
        code = "confirmation_stale" if change == "snapshot" else "authorization_denied"
        assert any(e.type is Event.EFFECT_FAILED and e.payload.get("code") == code for e in events)
        assert not rig.writes and not rig.attempted and not rig.verified
        assert [(name, stage) for name, stage, _ in debits if stage] == [("session.batch", "confirmed")]
        before_replay = list(debits)
        await confirm(session, preview.approval.plan_id)
        assert not rig.attempted and debits == before_replay
    asyncio.run(run())


@pytest.mark.parametrize("arguments", [
    batch(), batch(NAMES[0], BACKGROUND), batch(*NAMES[:2], BACKGROUND),
    batch(NAMES[0], "test.unknown"), batch(NAMES[0], "session.batch"),
    {"steps": [step(NAMES[0]), {"method": NAMES[1], "arguments": {"item": "wrong-schema"}}]},
    {"steps": [step(NAMES[0]), {"method": NAMES[1]}]},
    {"steps": [step(NAMES[0]), {**step(NAMES[1]), "extra": True}]},
    {"steps": [step(NAMES[0]), {"method": NAMES[1], "arguments": {"quality": True}}]},
    {**batch(*NAMES[:2]), "extra": True},
])
def test_invalid_or_unsupported_batch_is_rejected_before_writes(rig, arguments):
    async def run():
        _, view, events = await rig.start(arguments)
        assert view.approval is None
        wire = json.dumps([dict(e.payload) for e in events], ensure_ascii=False)
        assert ("unexpected keyword argument 'extra'" in wire if "extra" in arguments else "invalid_arguments" in wire)
        assert not rig.writes and not rig.attempted and not rig.verified
        assert await rig.session([]).pipeline.get_pending_plan(owner=OWNER, session_id=SESSION) is None
    asyncio.run(run())


def test_four_distinct_tools_fit_one_confirmation(rig):
    async def run():
        session, preview, _ = await rig.start(batch(*NAMES))
        assert preview.approval is not None and not rig.writes
        final, _ = await confirm(session, preview.approval.plan_id)
        assert final.effect_result["data"]["completed"] == 4
        assert rig.attempted == rig.verified == list(NAMES)
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["execute", "verify"])
def test_second_step_failure_keeps_first_receipt_and_stops_third(rig, failure, batch_quota):
    rig.rate_limiter, debits = batch_quota

    async def run():
        session, preview, _ = await rig.start(batch(*NAMES[:3]))
        (rig.fail_at if failure == "execute" else rig.fail_verify).add(NAMES[1])
        final, _ = await confirm(session, preview.approval.plan_id)
        data = final.effect_result["data"]
        assert final.status == "partial" and data["completed"] == 1 and data["total"] == 3
        assert rig.attempted == list(NAMES[:2])
        assert rig.values[NAMES[0]] == NAMES[0] and rig.values[NAMES[2]] == "before"
        assert len(rig.writes) == (1 if failure == "execute" else 2)
        assert rig.verified == list(NAMES[:1 if failure == "execute" else 2])
        first, failed, skipped = data["steps"]
        assert first["result"]["data"]["receipt"] == f"receipt:{NAMES[0]}"
        assert failed["result"]["status"] == "failed" and skipped["result"]["status"] == "not_started"
        assert [name for name, stage, _ in debits if stage] == ["session.batch", *NAMES[:2]]
        before_replay = list(debits)
        await confirm(session, preview.approval.plan_id)
        assert rig.attempted == list(NAMES[:2]) and debits == before_replay
    asyncio.run(run())


def test_unlisted_third_write_requires_its_own_approval(rig):
    async def run():
        session, preview, _ = await rig.start(followups=[call(NAMES[2], step(NAMES[2])["arguments"]), FINAL])
        next_view, _ = await confirm(session, preview.approval.plan_id)
        assert rig.attempted == list(NAMES[:2]) and rig.values[NAMES[2]] == "before"
        assert next_view.approval is not None
        assert next_view.status == "approval_required" and next_view.approval.tool_name == NAMES[2]
        assert next_view.approval is not None
        assert next_view.approval.plan_id != preview.approval.plan_id
        await confirm(session, preview.approval.plan_id)
        assert rig.attempted == list(NAMES[:2])
        pending = await session.pipeline.get_pending_plan(owner=OWNER, session_id=SESSION)
        assert pending is not None and pending.plan_id == next_view.approval.plan_id
    asyncio.run(run())


def test_ordinary_single_tool_still_requires_confirmation_and_verifies_once(rig):
    async def run():
        session, preview, events = await rig.start(step(NAMES[0])["arguments"], tool=NAMES[0])
        assert preview.approval.tool_name == NAMES[0] and not rig.writes
        assert sum(e.type is Event.EFFECT_APPROVAL_REQUIRED for e in events) == 1
        final, _ = await confirm(session, preview.approval.plan_id)
        assert final.status == "success" and final.effect_result["data"]["receipt"] == f"receipt:{NAMES[0]}"
        await confirm(session, preview.approval.plan_id)
        assert rig.attempted == rig.verified == [NAMES[0]] and len(rig.writes) == 1
    asyncio.run(run())


@pytest.mark.parametrize("drift", [False, True])
def test_cold_recovery_restores_public_plan_snapshots_and_consumption(rig, drift):
    async def run():
        original, _preview, preview_events = await rig.start()
        original_plan = await original.pipeline.get_pending_plan(owner=OWNER, session_id=SESSION)
        public = next(event.payload["plan"] for event in preview_events if event.type is Event.EFFECT_APPROVAL_REQUIRED)
        assert "editing" not in public  # nested batch steps没有可编辑标量，不发布空编辑能力。
        cold = rig.session([FINAL], SQLiteKernelStore(secret_provider=lambda: "task-test-only"))
        restored = await cold.pipeline.get_pending_plan(owner=OWNER, session_id=SESSION)
        assert restored is not None and restored.public_approval_dict() == original_plan.public_approval_dict()
        assert "editing" not in restored.public_approval_dict()
        assert not rig.writes and len(rig.trace) == 2
        if drift:
            rig.values[NAMES[1]] = "changed-after-restart"
        final, events = await confirm(cold, restored.plan_id)
        if drift:
            assert any(e.type is Event.EFFECT_FAILED and e.payload.get("code") == "confirmation_stale" for e in events)
            assert not rig.attempted and not rig.writes
        else:
            assert final.status == "success" and final.effect_result["data"]["completed"] == 2
            assert rig.attempted == rig.verified == list(NAMES[:2])
        again = rig.session([], SQLiteKernelStore(secret_provider=lambda: "task-test-only"))
        replay, _ = await confirm(again, restored.plan_id)
        assert replay.status == "failed" and len(rig.writes) == (0 if drift else 2)
        assert await again.pipeline.get_pending_plan(owner=OWNER, session_id=SESSION) is None
    asyncio.run(run())


def test_production_audit_records_each_step_before_finalizing(rig, monkeypatch):
    from app import database
    from app.agent.kernel.ports.mediaflux_effects import MediaFluxEffectLifecycle

    original = rig.session

    def audited_session(*args, **kwargs):
        session = original(*args, **kwargs)
        session.pipeline.effect_lifecycle = MediaFluxEffectLifecycle()
        session.pipeline.effect_store.record_actions = True
        return session

    monkeypatch.setattr(rig, "session", audited_session)

    async def run():
        session, preview, _ = await rig.start()
        final, _ = await confirm(session, preview.approval.plan_id)
        assert final.status == "success"
        with database.get_conn() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT tool_name,status,ok,confirmation_id FROM agent_action_history ORDER BY id")]
        assert len(rows) == 3
        assert [r["tool_name"] for r in rows] == ["session.batch", "unknown.confirmed_action", "unknown.confirmed_action"]
        assert all(r["ok"] and r["status"] == "completed" for r in rows)
        assert len({r["confirmation_id"] for r in rows}) == 3
        assert len(rig.writes) == 2

    asyncio.run(run())


def test_standalone_executor_freezes_one_batch_and_cold_claims_once(rig):
    async def run():
        catalog = ToolCatalog([rig.tool(name) for name in FIELDS])
        executor = EffectExecutor(catalog=catalog, state_store=rig.store,
            effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore()))
        lease, _ = await rig.store.begin_turn(owner=OWNER, session_id=SESSION, request_id="direct-effect")
        async def progress(_payload):
            pass
        context = ToolCallContext(OWNER, SESSION, lease.request_id, lease.turn_id, lease, CancellationToken(), progress)
        one = await executor.prepare(NAMES[0], {FIELDS[NAMES[0]]: "one"}, context=context, prepare_only=True)
        assert one.prepared_effect is not None and one.effect_plan is None
        assert await executor.get_pending_plan(owner=OWNER, session_id=SESSION) is None
        assert rig.writes == []
        preview = await executor.prepare("session.batch", batch(*NAMES[:2]), context=context)
        plan = preview.effect_plan
        assert plan is not None and rig.writes == []
        assert plan.public_approval_dict()["result"] == preview.outcome.public_content
        cold = EffectExecutor(catalog=ToolCatalog([rig.tool(name) for name in FIELDS]), state_store=rig.store,
            effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore()))
        restored = await cold.get_pending_plan(owner=OWNER, session_id=SESSION)
        assert restored is not None and restored.public_approval_dict() == plan.public_approval_dict()
        foreign_lease, _ = await rig.store.begin_turn(owner="another-owner", session_id=SESSION, request_id="foreign")
        with pytest.raises(ConfirmationClaimError):
            await cold.execute_confirmed(plan.plan_id, context=replace(context, owner="another-owner", lease=foreign_lease,
                request_id=foreign_lease.request_id, turn_id=foreign_lease.turn_id))
        completed = await cold.execute_confirmed(plan.plan_id, context=context)
        assert completed.outcome.public_content["ok"] is True
        assert rig.writes == [(name, {FIELDS[name]: name}) for name in NAMES[:2]]
        with pytest.raises(ConfirmationClaimError):
            await executor.execute_confirmed(plan.plan_id, context=context)
        assert rig.writes == [(name, {FIELDS[name]: name}) for name in NAMES[:2]]
    asyncio.run(run())


def test_legacy_frozen_parent_restores_without_old_prepare_route(rig):
    """仅构造迁移前存量计划；冷运行时没有旧模型工具或新预检入口。"""
    async def run():
        old = rig.session([]).pipeline
        lease, _ = await rig.store.begin_turn(owner=OWNER, session_id=SESSION, request_id="legacy-seed")
        async def progress(_):
            pass
        context = ToolCallContext(OWNER, SESSION, lease.request_id, lease.turn_id, lease, CancellationToken(), progress)
        native = old.catalog.get("session.batch")
        old.catalog.register(replace(native, name="agent.batch", model_name="", input_schema={"type": "object"}, validator=dict))
        arguments = {"steps": [{"tool": name, "arguments": step(name)["arguments"]} for name in NAMES[:2]]}
        saved = await old.prepare("agent.batch", arguments, context=context)
        assert saved.effect_plan is not None and saved.effect_plan.tool_name == "agent.batch"
        cold = rig.session([], SQLiteKernelStore(secret_provider=lambda: "task-test-only")).pipeline
        assert not cold.catalog.has("agent.batch")
        with pytest.raises(ToolPipelineError) as rejected:
            await cold.prepare("agent.batch", arguments, context=context)
        assert rejected.value.code == "tool_not_found"
        restored = await cold.get_pending_plan(owner=OWNER, session_id=SESSION)
        assert restored == saved.effect_plan
        foreign, _ = await cold.state_store.begin_turn(owner="other", session_id=SESSION, request_id="foreign")
        with pytest.raises(ConfirmationClaimError):
            await cold.execute_confirmed(restored.plan_id, context=replace(context, owner="other", lease=foreign))
        result = await cold.execute_confirmed(restored.plan_id, context=context)
        assert result.outcome.public_content["data"]["completed"] == 2
        assert [name for name, _ in rig.writes] == list(NAMES[:2])
        with pytest.raises(ConfirmationClaimError):
            await cold.execute_confirmed(restored.plan_id, context=context)
        assert len(rig.writes) == 2
    asyncio.run(run())


def test_unknown_child_fact_stops_without_replay(rig, monkeypatch):
    original = rig.tool
    def tool(name):
        value = original(name)
        if name != NAMES[1]:
            return value
        def unknown(*_):
            rig.attempted.append(name)
            error = ToolPipelineError("已受理但回执未知", code="outcome_unknown")
            error.mark_execution_started(True)
            raise error
        return replace(value, execute_confirmed=unknown)
    monkeypatch.setattr(rig, "tool", tool)
    async def run():
        session, preview, _ = await rig.start(batch(*NAMES[:3]))
        result, _ = await confirm(session, preview.approval.plan_id)
        first, unknown, tail = result.effect_result["data"]["steps"]
        assert first["result"]["status"] == "completed"
        assert unknown["result"]["status"] == "outcome_unknown"
        assert tail["result"]["status"] == "not_started"
        await confirm(session, preview.approval.plan_id)
        assert rig.attempted == list(NAMES[:2]) and len(rig.writes) == 1
    asyncio.run(run())
