"""F09：事后 READ 回执显式关联；共用配置账本、SQLite CAS 和真实 Kernel。"""
from tests.agent_task_native_helpers import task_call, returned_task
from typing import Any

import asyncio
import json
import socket
from copy import deepcopy
from dataclasses import asdict, replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.agent.kernel import bootstrap, task_state
from app.agent.kernel.bootstrap import build_agent_kernel_runtime, build_tool_catalog
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.conversation_state import retain_conversation
from app.agent.kernel.events import AgentEventType
from app.agent.kernel.model import ModelEvent, ModelEventType, ModelToolCall
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.session import AgentSession
from app.agent.kernel.state_types import AgentInput
from app.agent.models import ToolResult
from tests.test_agent_kernel_core import ScriptedModel, read_tool
from tests.test_agent_recovered_progress import reply
from tests.test_agent_task_cas import command, observe
from tests.test_agent_task_cas import task_store as isolated_store
from tests.test_agent_task_first_slice import OWNER, declaration, configuration_call
from tests.test_agent_tool_argument_ingress import Wire

GOAL = "观察 TMDB 配置，读取 Jellyfin 库名和类型；仅观察、不修改。"
TOOL = "fixture.library_read"
ARGS = {"query": "libraries"}
OTHER = {"query": "counts"}
SUMMARY = "读取到 Jellyfin 库名和类型"
SUCCESS = {"ok": True, "status": "success", "summary": SUMMARY, "data": {"items": []}}
task_store = isolated_store


def library_read(handler, *, parallel=False):
    return replace(
        read_tool(TOOL, handler=handler),
        input_schema={"type": "object", "properties": {"query": {"type": "string"}},
                      "required": ["query"], "additionalProperties": False},
        metadata={"parallel_read": parallel},
    )


def declared(*, mixed=False, goals=("libraries",)):
    value = declaration()
    value.update(source=GOAL, root_goal=GOAL)
    value["obligations"] = value["obligations"][:1] if mixed else []
    value["obligations"] += [
        {"id": goal, "description": "观察 " + goal, "required": True,
         "acceptance": {"kind": "observed_read", "criterion": "读取 Jellyfin 库名和类型：" + goal}}
        for goal in goals
    ]
    return value


def control(value, task=None):
    return task_state.proposal(value, SimpleNamespace(
        request_id="read-request", user_source=GOAL, current_task=task,
    ))


def apply(task, task_patch):
    return task_state.reduce_task(task, task_patch, owner=OWNER, session_id="cas")


def new_task(**kwargs):
    return apply(None, control(declared(**kwargs)))


def read_patch(task, result=SUCCESS, *, arguments=ARGS, call_id="read-call"):
    observations = task_state.read_observations(task, TOOL, deepcopy(arguments), deepcopy(result))
    return task_state.observation_patch(
        task, owner=OWNER, session_id="cas", tool=TOOL, call_id=call_id, observations=observations,
    )


def read(task, result=SUCCESS, **kwargs):
    task_patch = read_patch(task, result, **kwargs)
    return apply(task, task_patch) if task_patch is not None else task


def checkpoint(task, bindings=None, *, evidence_ids=None):
    values = {"evidence_ids": ([f["evidence_id"] for f in task["verified_facts"]]
                               if evidence_ids is None else evidence_ids), "blockers": []}
    if bindings is not None:
        values["read_evidence"] = bindings
    return apply(task, command(task, "checkpoint", **values))


def reads(task):
    return [f for f in task["verified_facts"] if "query_key" in f]


def tool_round(calls):
    calls = [configuration_call(tool, args) if tool == "diagnostics.component" else (tool, args) for tool, args in calls]
    return [*(ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall(
        f"call-{index}-{tool}", tool, args,
    )) for index, (tool, args) in enumerate(calls)),
        ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")]


class TaskModel(ScriptedModel):
    """复用脚本模型，只在收到实际回执后生成 checkpoint；不代替任务执行器。"""

    def __init__(self, calls, *, goals=()):
        super().__init__([tool_round([("agent.run_code", {
            "language": "shell", "code": "mf session.checkpoint_task --help",
        })]), tool_round(calls)])
        self.goals = goals

    async def stream(self, request, *, cancellation):
        if not self.rounds:
            if self.goals is not None:
                outputs = [json.loads(m.content) for m in request.messages if m.role == "tool"]
                task = next(t for v in reversed(outputs) if (t := returned_task(v)))
                facts = [f for f in reads(task) if f["tool"] == TOOL]
                args: dict[str, Any] = {"operation": "checkpoint", "blockers": [],
                        "evidence_ids": [f["evidence_id"] for f in task["verified_facts"]]}
                if self.goals:
                    args["read_evidence"] = dict(zip(self.goals, [f["evidence_id"] for f in facts]))
                self.rounds.append(tool_round([task_call(args)]))
            self.rounds.append([ModelEvent(ModelEventType.TEXT_DELTA, text="已报告观察，不推断全量或写入完成。"),
                                ModelEvent(ModelEventType.FINISH, finish_reason="stop")])
        async for event in super().stream(request, cancellation=cancellation):
            yield event


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("F09 tests must not access the network")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)


@pytest.fixture
def backend(task_store, monkeypatch):
    calls = []
    def query(arguments, context):
        calls.append(deepcopy(arguments))
        return ToolResult(**deepcopy(SUCCESS))
    def catalog():
        original = build_tool_catalog()
        original.register(library_read(query))
        return original
    monkeypatch.setattr(bootstrap, "build_tool_catalog", catalog)
    return calls


async def seed(store, value):
    lease, _ = await store.begin_turn(owner=OWNER, session_id="cas", request_id="read-request")
    return await store.commit(lease, task_patch=control(value))


async def turn(store, calls, *, goals=(), message=GOAL):
    model = TaskModel(calls, goals=goals)
    runtime = build_agent_kernel_runtime(model=model, store=store)
    runtime.session.model_tools = (runtime.session.model_tools or frozenset()) | {TOOL}
    events = [e async for e in runtime.session.run(AgentInput(message=message, owner=OWNER, session_id="cas"))]
    saved = await store.load(owner=OWNER, session_id="cas")
    assert not any(e.type.value == "tool.failed" for e in events)
    return saved, model, events


@pytest.mark.parametrize("configuration_first", [True, False])
def test_configuration_history_and_two_domain_cold_recovery(task_store, backend, configuration_first):
    async def run():
        await seed(task_store, declared(mixed=True))
        config_call = ("diagnostics.component", {"component": "tmdb"})
        first = config_call if configuration_first else (TOOL, ARGS)
        second = (TOOL, ARGS) if configuration_first else config_call
        saved, _, events = await turn(task_store, [first], goals=() if configuration_first else ("libraries",))
        task = saved.metadata["task"]
        assert task["status"] == events[-1].payload["task"]["status"] == "partial"
        assert [o["state"] for o in task["obligations"]] == (
            ["verified", "pending"] if configuration_first else ["pending", "verified"])
        retained = retain_conversation(saved.conversation + [{"role": "user", "content": "保留的新回合"}], maximum_bytes=100)
        assert GOAL not in json.dumps(retained, ensure_ascii=False)
        lease, _ = await task_store.begin_turn(owner=OWNER, session_id="cas", request_id="prune")
        await task_store.commit(lease, conversation=retained)
        fresh = SQLiteKernelStore(secret_provider=lambda: "task-test-only")
        assert (await fresh.load(owner=OWNER, session_id="cas")).metadata["task"] == task
        end, model, events = await turn(fresh, [second], goals=("libraries",) if configuration_first else (), message="继续")
        final = end.metadata["task"]
        assert final["status"] == events[-1].payload["task"]["status"] == "complete"
        assert final["task_id"] == task["task_id"] and final["epoch"] == task["epoch"]
        assert all(f in final["verified_facts"] for f in task["verified_facts"] if any(
            f["evidence_id"] in o["evidence_ids"] for o in task["obligations"]))
        assert GOAL in json.dumps(asdict(model.requests[0]), ensure_ascii=False)
        assert [{k:a[k] for k in ARGS} for a in backend] == [ARGS]
        business_calls = [e.payload["tool"] for e in events if e.type.value == "tool.started" and not e.payload["tool"].startswith("agent.")]
        if second[0] == "diagnostics.component":
            sdk_calls = [call["method"] for event in events if event.type.value == "tool.completed"
                         for call in event.payload["result"].get("data", {}).get("sdk_calls", [])]
            assert sdk_calls == [second[0], "session.checkpoint_task"] and business_calls == []
        else:
            assert business_calls == [second[0]]
        [configuration] = [f for f in final["verified_facts"] if f.get("component") == "tmdb"]
        assert configuration["verifier_version"] == "configuration-v1"
    asyncio.run(run())


def test_actual_receipt_requires_explicit_association_not_just_evidence_ids():
    task = new_task(goals=("libraries", "remaining"))
    result = {**SUCCESS, "data": {"items": [{"raw": "do-not-copy-result"}], "has_more": True}}
    task = checkpoint(read(task, result))
    [fact] = reads(task)
    assert all(o["state"] == "pending" for o in task["obligations"])
    assert fact["tool"] == TOOL and fact["summary"] and fact["status"] == "success"
    assert fact["query_key"].startswith("read:") and len(fact["summary"]) <= 240
    assert "do-not-copy-result" not in json.dumps(task)
    task = checkpoint(task, {"libraries": fact["evidence_id"]})
    assert [o["state"] for o in task["obligations"]] == ["verified", "pending"]
    assert task["status"] == "partial"
    task_projection_result = task_state.task_projection(task)
    assert task_projection_result is not None
    assert task_projection_result["remaining_obligation_ids"] == ["remaining"]


def test_read_link_does_not_require_duplicating_receipt_in_configuration_ids():
    task = read(new_task())
    [fact] = reads(task)
    assert checkpoint(task, {"libraries": fact["evidence_id"]}, evidence_ids=[])["status"] == "complete"


@pytest.mark.parametrize("invalid", ["unknown-goal", "fake-id", "config-receipt"])
def test_checkpoint_rejects_invalid_associations(invalid):
    task = new_task(mixed=True)
    task = read(task)
    task = apply(task, observe(task))
    read_id = reads(task)[0]["evidence_id"]
    bindings, selected = {"libraries": read_id}, None
    if invalid == "unknown-goal":
        bindings = {"unknown": read_id}
    elif invalid == "fake-id":
        bindings = {"libraries": "forged"}
    else:
        bindings = {"libraries": next(f["evidence_id"] for f in task["verified_facts"] if "component" in f)}
    before = deepcopy(task)
    with pytest.raises(ValueError):
        checkpoint(task, bindings, evidence_ids=selected)
    assert task == before


@pytest.mark.parametrize("status", ["failed", "partial", "pending", "queued", "running"])
def test_failed_or_queued_read_stays_pending_without_poisoning_future_success(status):
    task = read(new_task(), {**SUCCESS, "status": status, "ok": status != "failed"})
    assert task["verified_facts"] == [] and task["blockers"] == []
    assert checkpoint(task)["obligations"][0]["state"] == "pending"
    task = read(task)
    [fact] = reads(task)
    assert checkpoint(task, {"libraries": fact["evidence_id"]})["status"] == "complete"


@pytest.mark.parametrize("field,value", [
    ("owner", "other-owner"), ("session_id", "other-session"),
    ("task_id", "other-task"), ("epoch", 999),
])
def test_foreign_scope_receipts_cannot_be_associated(field, value):
    task = read(new_task())
    fact = reads(task)[0]
    fact["scope"][field] = value
    with pytest.raises(ValueError):
        checkpoint(task, {"libraries": fact["evidence_id"]})


def test_sqlite_read_receipts_keep_same_cas_and_revision_epoch_fences(task_store):
    async def run():
        initial = await seed(task_store, declared())
        lease, _ = await task_store.begin_turn(owner=OWNER, session_id="cas", request_id="observe")
        task = initial.metadata["task"]
        first, stale = read_patch(task), read_patch(task, call_id="stale")
        saved = await task_store.commit(lease, task_patch=first)
        with pytest.raises(task_state.TaskConflict):
            await task_store.commit(lease, task_patch=stale)
        assert (await task_store.load(owner=OWNER, session_id="cas")).metadata["task"] == saved.metadata["task"]
        old_id = reads(saved.metadata["task"])[0]["evidence_id"]
        revised = await task_store.commit(lease, task_patch=control(
            {**declared(), "operation": "revise"}, saved.metadata["task"]))
        current = revised.metadata["task"]
        assert current["epoch"] == 2 and current["verified_facts"] == []
        with pytest.raises(task_state.TaskConflict):
            await task_store.commit(lease, task_patch=stale)
        with pytest.raises(ValueError):
            checkpoint(current, {"libraries": old_id}, evidence_ids=[old_id])
        assert (await task_store.load(owner=OWNER, session_id="cas")).metadata["task"] == current
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["tracked", "untracked", "configuration-only"])
def test_same_round_tracked_reads_keep_both_receipts_and_ordinary_reads_stay_parallel(task_store, backend, mode):
    async def run():
        active = peak = 0
        calls, together = [], asyncio.Event()
        async def handler(arguments, context):
            nonlocal active, peak
            calls.append(deepcopy(arguments))
            active += 1
            peak = max(peak, active)
            if active == 2:
                together.set()
            try:
                if mode != "tracked":
                    await asyncio.wait_for(together.wait(), timeout=2)
                else:
                    await asyncio.sleep(0)
                return ToolResult(**deepcopy(SUCCESS))
            finally:
                active -= 1
        def catalog():
            original = build_tool_catalog()
            original.register(library_read(handler, parallel=True))
            return original
        if mode != "untracked":
            await seed(task_store, declared(mixed=mode == "configuration-only",
                                           goals=() if mode == "configuration-only" else ("libraries", "counts")))
        with patch.object(bootstrap, "build_tool_catalog", catalog):
            saved, _, events = await turn(task_store, [(TOOL, ARGS), (TOOL, OTHER)],
                                         goals=("libraries", "counts") if mode == "tracked" else None)
        assert [{k:a[k] for k in ARGS} for a in calls] == [ARGS, OTHER] and active == 0
        assert peak == (1 if mode == "tracked" else 2)
        if mode == "tracked":
            task = saved.metadata["task"]
            assert task["status"] == events[-1].payload["task"]["status"] == "complete"
            assert len(reads(task)) == len({f["query_key"] for f in reads(task)}) == 2
            assert len({f["call_id"] for f in reads(task)}) == 2
            assert all(o["state"] == "verified" and len(o["evidence_ids"]) == 1 for o in task["obligations"])
        elif mode == "untracked":
            assert "task" not in saved.metadata
        else:
            assert saved.metadata["task"]["verified_facts"] == []
    asyncio.run(run())


@pytest.mark.parametrize("distinct", [False, True])
def test_forty_read_observations_are_count_and_byte_bounded_preserving_linked_receipts(distinct):
    task = read(new_task(goals=("libraries", "remaining")))
    linked = reads(task)[0]["evidence_id"]
    task = checkpoint(task, {"libraries": linked})
    for index in range(40):
        arguments = {**ARGS, "arguments": {"page": index if distinct else 0}}
        result = {**SUCCESS, "summary": "只读观测摘要中文内容" * 45}
        task = read(task, result, arguments=arguments, call_id=f"stress-{index}")
        assert len(task["verified_facts"]) <= 32
        assert len(json.dumps(task, ensure_ascii=False, separators=(",", ":")).encode()) <= 16 * 1024
        assert any(f["evidence_id"] == linked for f in reads(task))
        assert task["obligations"][0]["evidence_ids"] == [linked]
        assert task["obligations"][1]["state"] == "pending"
    if not distinct:
        assert len(reads(task)) <= 3


@pytest.mark.parametrize("tracked", [False, True])
def test_short_model_data_survives_task_receipt_through_native_wire_and_cold_sqlite(task_store, tracked):
    async def run():
        if tracked:
            await seed(task_store, declared())
        dto = ToolResult(ok=True, status="success", summary="读取完成",
                         data={"public_only": True}, model_data={"model_only": True})
        tool = read_tool("records.inspect", handler=lambda *_: dto)
        catalog = ToolCatalog([tool])
        wire = Wire("chat_completions", [
            reply("chat_completions", name=tool.model_name, arguments={}),
            reply("chat_completions", text="已依据实际观察回答。"),
        ])
        session = AgentSession(model=wire.adapter, catalog=catalog,
            model_tools=[tool.name],
            pipeline=ToolPipeline(catalog=catalog, state_store=task_store), state_store=task_store)
        completed = []
        async for event in session.run(AgentInput(message=GOAL, owner=OWNER, session_id="cas")):
            assert event.type not in {AgentEventType.TOOL_FAILED, AgentEventType.TURN_FAILED}
            if event.type is AgentEventType.TOOL_COMPLETED:
                completed.append(event)
                cold = await SQLiteKernelStore(secret_provider=lambda: "task-test-only").load(owner=OWNER, session_id="cas")
                [saved] = [row for row in cold.conversation if row.get("role") == "tool"]
                saved_payload = json.loads(saved["content"])
                assert saved_payload["data"] == dto.model_data
                if tracked:
                    assert saved_payload["task"] == event.payload["result"]["task"] == task_state.task_projection(cold.metadata["task"])
                    assert reads(cold.metadata["task"])[0]["call_id"] == saved["tool_call_id"]
        wire.verify(2)
        [public] = [event.payload["result"] for event in completed]
        [message] = [row for row in wire.requests[-1]["messages"] if row["role"] == "tool"]
        model_payload = json.loads(message["content"])
        assert model_payload["data"] == dto.model_data
        assert public["data"] == dto.data
        assert "result_handle" not in model_payload and "result_handle" not in public
        assert ("task" in model_payload) is tracked
        [assistant] = [row for row in wire.requests[-1]["messages"] if row.get("tool_calls")]
        assert assistant["tool_calls"][0]["id"] == message["tool_call_id"]
    asyncio.run(run())


def test_read_task_patch_commit_conflict_never_publishes_success_or_earns_progress(task_store, monkeypatch):
    from app.agent.kernel.adapters import TurnViewBuilder

    async def run():
        initial = await seed(task_store, declared())
        original_commit = task_store.commit
        rejected = []

        async def conflict(lease, **kwargs):
            task_patch = kwargs.get("task_patch")
            if task_patch is not None and task_patch.operation == "observe":
                rejected.append(deepcopy(kwargs["conversation"]))
                # 预览成功后，在真实 SQLite 事务中触发 revision 冲突。
                kwargs["task_patch"] = replace(task_patch, expected={
                    **task_patch.expected, "revision": task_patch.expected["revision"] + 1,
                })
            return await original_commit(lease, **kwargs)

        monkeypatch.setattr(task_store, "commit", conflict)
        marker = "uncommitted-read-result"
        tool = read_tool("records.inspect", handler=lambda *_: {
            **SUCCESS, "data": {"marker": marker},
        })
        catalog = ToolCatalog([tool])
        answer = "任务检查点冲突，尚未完成。"
        model = ScriptedModel([tool_round([(tool.name, {})]), [
            ModelEvent(ModelEventType.TEXT_DELTA, text=answer),
            ModelEvent(ModelEventType.FINISH, finish_reason="stop"),
        ]])
        session = AgentSession(model=model, catalog=catalog,
            model_tools=[tool.name],
            pipeline=ToolPipeline(catalog=catalog, state_store=task_store), state_store=task_store)
        events = [event async for event in session.run(AgentInput(message=GOAL, owner=OWNER, session_id="cas"))]
        cold = await SQLiteKernelStore(secret_provider=lambda: "task-test-only").load(owner=OWNER, session_id="cas")
        assert len(rejected) == 1 and marker in json.dumps(rejected)
        assert cold.metadata["task"] == initial.metadata["task"]
        assert not any(event.type is AgentEventType.TOOL_COMPLETED for event in events)
        [failure] = [event for event in events if event.type is AgentEventType.TOOL_FAILED]
        assert failure.payload["code"] == "task_conflict"
        # 响应交付与任务完成分开：普通 READ 失败由模型说明，不覆写为第二段答案。
        terminal = events[-1]
        assert terminal.type is AgentEventType.TURN_COMPLETED
        assert terminal.payload["status"] == terminal.payload["turn_status"] == "partial"
        task = terminal.payload["task"]
        assert task == task_state.task_projection(cold.metadata["task"])
        assert task["status"] == "partial" and task["verified_facts"] == []
        assert task["remaining_obligation_ids"] == ["libraries"]
        assert all(obligation["state"] == "pending" for obligation in task["obligations"])
        builder = TurnViewBuilder()
        for event in events:
            builder.apply(event)
        view = builder.build()
        assert view.terminal and view.status == "partial" and view.task == task
        assert view.answer == terminal.payload["answer"] == answer
        assert "".join(event.payload["delta"] for event in events
                       if event.type is AgentEventType.MODEL_DELTA) == answer
        assert marker not in json.dumps(cold.conversation)
        [call] = [call for row in cold.conversation for call in row.get("tool_calls", [])]
        [result] = [row for row in cold.conversation if row.get("role") == "tool"]
        assert result["tool_call_id"] == call["call_id"]
        assert json.loads(result["content"])["code"] == "task_conflict"
        assert events[-1].payload["successful_read_queries"] == 0
        assert len(model.requests) == 2
        assert len([message for message in model.requests[-1].messages if message.role == "tool"]) == 1
    asyncio.run(run())
