"""原生会话任务：真实root工作区、SQLite发布和原验收证据。"""
import asyncio
import json
import re
import socket
import threading
from hashlib import sha256

import pytest

from app import sdk
from app.agent.feature_gate import initialize_runtime_fence
from app.agent.kernel import sandbox
from app.agent.kernel.bootstrap import (
    _capture_creation_binding,
    build_agent_kernel_runtime,
)
from app.agent.kernel.model import ModelEvent, ModelEventType, ModelToolCall
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.state_types import AgentInput
from app.clients.openai_compatible import parse_native_call
from tests import test_agent_session_result as native_tests
from tests import test_agent_task_cas as cas_tests
from tests.test_agent_kernel_core import ScriptedModel
from tests.test_agent_task_first_slice import GOAL, OWNER, declaration

native_workspace = native_tests.native_workspace
task_store = cas_tests.task_store

def code_round(code, *, language="python", call_id="native-task"):
    return [ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall(
        call_id, "agent.run_code", {"code": code, "language": language})),
        ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")]


def final():
    return [ModelEvent(ModelEventType.TEXT_DELTA, text="已记录真实核验。"),
            ModelEvent(ModelEventType.FINISH, finish_reason="stop")]


@pytest.mark.parametrize("fail_first", [False, True])
def test_same_script_start_read_checkpoint_and_cli(task_store, native_workspace, monkeypatch, fail_first):
    real_calls = []
    def component(component):
        real_calls.append(component)
        if fail_first and len(real_calls) == 1:
            raise ValueError("离线配置读取失败")
        return {"ok": True, "status": "not_configured", "data": {
            "component": component, "status": "not_configured", "enabled": False,
            "probe_mode": "configuration_only", "network_accessed": False}}
    monkeypatch.setitem(sdk.METHODS, "diagnostics.component", component)
    monkeypatch.setattr(socket.socket, "connect", lambda *_: pytest.fail("禁止外联"))
    args = declaration()
    args.pop("operation")
    code = f'''import json
from mf import session, diagnostics
t = session.start_task(**{args!r})['data']['task']
assert t['revision'] == 1 and t['status'] == 'partial'
assert session.task()['data']['task'] == t
if {fail_first!r}:
    try:
        diagnostics.component('tmdb')
    except mf.Error:
        pass
    else:
        raise AssertionError('必须保留真实失败')
    failed = session.task()['data']['task']
    assert failed['status'] == 'blocked' and not failed['verified_facts']
for c in ('tmdb', 'qbittorrent'):
    assert diagnostics.component(c)['ok']
t = session.task()['data']['task']
assert len(t['verified_facts']) == 2
assert not any(o['state'] == 'verified' for o in t['obligations'])
done = session.checkpoint_task([f['evidence_id'] for f in t['verified_facts']], [])
assert done['data']['task']['status'] == 'complete'
print(json.dumps(done))'''
    async def run():
        initialize_runtime_fence()
        model = ScriptedModel([code_round(code), final()])
        runtime = build_agent_kernel_runtime(model=model, store=task_store)
        runtime.session.continuation_binding = _capture_creation_binding()
        assert not runtime.session.catalog.has("agent.task")
        events = [e async for e in runtime.session.run(AgentInput(
            message=GOAL, owner=OWNER, session_id="native-task"))]
        stored = await task_store.load(owner=OWNER, session_id="native-task")
        result = next(e.payload["result"] for e in events if e.type.value == "tool.completed")
        assert result["data"]["exit_code"] == 0, result['data']['stderr']
        assert events[-1].payload["task"]["status"] == stored.metadata["task"]["status"] == "complete"
        assert real_calls == (["tmdb"] if fail_first else []) + ["tmdb", "qbittorrent"]
        assert stored.metadata['read_wait']['budget']['model_spent'] == 2
        assert stored.metadata['read_wait']['budget']['tool_spent'] == 1
        cold_model = ScriptedModel([code_round("mf session.task\nmf session.start_task --help", language="shell"), final()])
        cold = build_agent_kernel_runtime(model=cold_model, store=task_store)
        later = [e async for e in cold.session.run(AgentInput(
            message="读取原验收状态", owner=OWNER, session_id="native-task"))]
        readback = next(e.payload["result"]["data"] for e in later if e.type.value == "tool.completed")
        assert readback['exit_code'] == 0, readback
        assert 'root_goal' in readback['stdout'] and 'constraints' in readback['stdout']
        assert json.JSONDecoder().raw_decode(readback['stdout'])[0]['data']['task']['task_id'] == stored.metadata['task']['task_id']
    asyncio.run(run())


@pytest.mark.parametrize("call_id_length", [62, 63, 64])
def test_only_business_reads_earn_native_task_evidence(task_store, native_workspace, monkeypatch, call_id_length):
    queried = []

    def search(query: str):
        queried.append(query)
        return {"ok": True, "status": "success", "summary": "真实离线查询", "items": []}

    monkeypatch.setitem(sdk.METHODS, "media.search", search)
    monkeypatch.setattr(socket.socket, "connect", lambda *_: pytest.fail("禁止外联"))
    goal = "核对媒体数据，可保存方法笔记和调整展示，仅观察业务。"
    args = declaration()
    args.pop("operation")
    args.update(source=goal, root_goal=goal, obligations=[{
        "id": "media", "description": "取得本次查询", "required": True,
        "acceptance": {"kind": "observed_read", "criterion": "取得真实媒体查询结果"},
    }])
    code = f'''import json
from mf import session, skills, resources, media
session.start_task(**{args!r})
first = skills.save('research', '已验证的方法', '# 第一版方法')['item']
second = skills.save('research', '已验证的方法', '# 第二版方法', first['revision'])['item']
restored = skills.restore('research', first['revision'], second['revision'])['item']
resources.present([])
session.pending_plan()
before = session.task()['data']['task']
media.search('sample')
observed = session.task()['data']['task']
[fact] = [f for f in observed['verified_facts'] if f['tool'] == 'media.search']
done = session.checkpoint_task([], [], read_evidence={{'media': fact['evidence_id']}})
print(json.dumps({{'before': before, 'observed': observed, 'done': done,
                  'restored': restored, 'first': first}}, ensure_ascii=False))'''

    async def run():
        initialize_runtime_fence()
        native = parse_native_call("c" * call_id_length, "agent__run_code", json.dumps({"code": code}),
                                   allow_argument_errors=True)
        assert native.call_id == "c" * call_id_length
        runtime = build_agent_kernel_runtime(model=ScriptedModel([
            code_round(code, call_id=native.call_id), final(),
        ]), store=task_store)
        events = [event async for event in runtime.session.run(AgentInput(
            message=goal, owner=OWNER, session_id="native-read-evidence"))]
        [result] = [event.payload["result"]["data"] for event in events if event.type.value == "tool.completed"]
        assert result["exit_code"] == 0, result["stderr"]
        payload = json.loads(result["stdout"])
        assert payload["before"]["status"] == "partial" and payload["before"]["verified_facts"] == []
        assert [fact["tool"] for fact in payload["observed"]["verified_facts"]] == ["media.search"]
        assert payload["restored"]["content"] == payload["first"]["content"]
        assert payload["restored"]["revision"] > payload["first"]["revision"]
        assert payload["done"]["data"]["task"]["status"] == events[-1].payload["task"]["status"] == "complete"
        cold = await SQLiteKernelStore(secret_provider=lambda: "task-test-only").load(
            owner=OWNER, session_id="native-read-evidence")
        assert [fact["tool"] for fact in cold.metadata["task"]["verified_facts"]] == ["media.search"]
        [fact] = cold.metadata["task"]["verified_facts"]
        assert len(fact["call_id"]) <= 64 and fact["evidence_id"] != fact["call_id"]
        assert [row["tool_call_id"] for row in cold.conversation if row["role"] == "tool"] == [native.call_id]
        assert [call["call_id"] for row in cold.conversation for call in row.get("tool_calls", [])] == [native.call_id]
        assert queried == ["sample"]
        assert [call["method"] for call in result["sdk_calls"]] == [
            "session.start_task", "skills.save", "skills.save", "skills.restore", "resources.present",
            "session.pending_plan", "session.task", "media.search", "session.task", "session.checkpoint_task",
        ]

    asyncio.run(run())


def test_parallel_sdk_request_identity_survives_cold_runtime(task_store, native_workspace, monkeypatch):
    queries = ["parallel-0", "parallel-1", "parallel-2"]
    entered = threading.Barrier(3)
    finished = [threading.Event() for _ in queries]
    calls = []

    def search(query: str):
        if query in queries:
            index = queries.index(query)
            entered.wait(timeout=5)
            if index < 2:
                assert finished[index + 1].wait(5)
            calls.append(query)
            finished[index].set()
        else:
            calls.append(query)
        return {"ok": True, "status": "success", "summary": query, "items": []}

    monkeypatch.setitem(sdk.METHODS, "media.search", search)
    goal = "并发只读核对媒体，冷恢复后继续查询。"
    args = declaration()
    args.pop("operation")
    args.update(source=goal, root_goal=goal, obligations=[{
        "id": "media", "description": "取得媒体查询证据", "required": True,
        "acceptance": {"kind": "observed_read", "criterion": "返回真实查询结果"},
    }])
    first_code = f'''import json
from concurrent.futures import ThreadPoolExecutor
from mf import session, media
session.start_task(**{args!r})
with ThreadPoolExecutor(3) as pool:
    list(pool.map(media.search, {queries!r}))
print(json.dumps(session.task()['data']['task']))'''
    cold_code = '''from mf import session, media
media.search('after-cold')
t = session.task()['data']['task']
session.checkpoint_task([], [], read_evidence={'media': t['verified_facts'][-1]['evidence_id']})'''

    async def run():
        initialize_runtime_fence()
        runtime = build_agent_kernel_runtime(model=ScriptedModel([
            code_round(first_code, call_id="parallel-sdk"), final(),
        ]), store=task_store)
        events = [e async for e in runtime.session.run(AgentInput(message=goal, owner=OWNER, session_id="rpc-identities"))]
        [result] = [e.payload["result"]["data"] for e in events if e.type.value == "tool.completed"]
        assert result["exit_code"] == 0, result["stderr"]
        cold_store = SQLiteKernelStore(secret_provider=lambda: "task-test-only")
        before = (await cold_store.load(owner=OWNER, session_id="rpc-identities")).metadata["task"]["verified_facts"]
        ids = {fact["call_id"] for fact in before}
        assert len(before) == len(ids) == 3
        assert all(re.fullmatch(r"[0-9a-f]{32}\.json", value) for value in ids)
        activities = [e.payload["activity"] for e in events if e.payload.get("activity", {}).get("method") == "media.search"]
        assert {activity["id"] for activity in activities} == ids
        assert all([a["status"] for a in activities if a["id"] == value] == ["running", "success"] for value in ids)
        cold = build_agent_kernel_runtime(model=ScriptedModel([
            code_round(cold_code, call_id="cold-sdk"), final(),
        ]), store=cold_store)
        later = [e async for e in cold.session.run(AgentInput(message="继续只读核对", owner=OWNER, session_id="rpc-identities"))]
        [result] = [e.payload["result"]["data"] for e in later if e.type.value == "tool.completed"]
        assert result["exit_code"] == 0, result["stderr"]
        after = (await SQLiteKernelStore(secret_provider=lambda: "task-test-only").load(
            owner=OWNER, session_id="rpc-identities")).metadata["task"]
        assert after["status"] == "complete" and after["verified_facts"][:3] == before
        assert len({fact["call_id"] for fact in after["verified_facts"]}) == 4
        assert len({fact["evidence_id"] for fact in after["verified_facts"]}) == 4
        assert all(fact["evidence_id"] != fact["call_id"] for fact in after["verified_facts"])
        fresh = after["verified_facts"][-1]["call_id"]
        assert re.fullmatch(r"[0-9a-f]{32}\.json", fresh) and fresh not in ids
        assert {e.payload["activity"]["id"] for e in later
                if e.payload.get("activity", {}).get("method") == "media.search"} == {fresh}
        assert calls == [*reversed(queries), "after-cold"]

    asyncio.run(run())


def test_real_recommend_signature_omits_sentinels_but_keeps_explicit_empty_zero_false(
    task_store, native_workspace, monkeypatch,
):
    received = []

    def recommend(**arguments):
        received.append(arguments)
        return {"ok": True, "status": "success", "summary": "真实离线推荐", "items": []}

    monkeypatch.setattr("app.modules.media_library.recommend", recommend)
    monkeypatch.setattr(socket.socket, "connect", lambda *_: pytest.fail("禁止外联"))
    goal = "只读核对本地推荐，分别沿用偏好和显式清空过滤条件。"
    args = declaration()
    args.pop("operation")
    args.update(source=goal, root_goal=goal, obligations=[{
        "id": "recommend", "description": "读取真实推荐", "required": True,
        "acceptance": {"kind": "observed_read", "criterion": "取得推荐回执"},
    }])
    explicit = {"must_match": [], "prefer": [], "exclude": [], "min_rating": 0, "exclude_played": False}
    code = f'''import json
from mf import session, library
session.start_task(**{args!r})
library.recommend()
library.recommend(**{explicit!r})
observed = session.task()['data']['task']
done = session.checkpoint_task([], [], read_evidence={{'recommend': observed['verified_facts'][-1]['evidence_id']}})
print(json.dumps({{'observed': observed, 'done': done}}, ensure_ascii=False))'''

    async def run():
        initialize_runtime_fence()
        runtime = build_agent_kernel_runtime(model=ScriptedModel([code_round(code), final()]), store=task_store)
        events = [event async for event in runtime.session.run(AgentInput(
            message=goal, owner=OWNER, session_id="native-recommend-evidence"))]
        [result] = [event.payload["result"]["data"] for event in events if event.type.value == "tool.completed"]
        assert result["exit_code"] == 0, result["stderr"]
        payload = json.loads(result["stdout"])
        expected = [{"server": "auto", "media_type": "any", "limit": 8},
                    {"server": "auto", "media_type": "any", "limit": 8, **explicit}]
        assert [{key: value for key, value in call.items() if key not in {"preferences", "cancelled"}}
                for call in received] == expected
        assert all(isinstance(call["preferences"], dict) for call in received)
        facts = payload["observed"]["verified_facts"]
        assert [fact["tool"] for fact in facts] == ["library.recommend", "library.recommend"]
        assert [fact["query_key"] for fact in facts] == ["read:" + sha256(json.dumps(
            ["library.recommend", arguments], sort_keys=True, ensure_ascii=False,
            separators=(",", ":")).encode()).hexdigest() for arguments in expected]
        workspace, _ = sandbox._workspace_location(OWNER, "native-recommend-evidence")
        archived = [json.loads((workspace / call["response_path"].lstrip("/")).read_text())
                    for call in result["sdk_calls"] if call["method"] == "library.recommend"]
        assert [receipt["arguments"] for receipt in archived] == expected
        assert all(receipt["result"]["ok"] is True for receipt in archived)
        assert payload["done"]["data"]["task"]["status"] == events[-1].payload["task"]["status"] == "complete"
        cold = await SQLiteKernelStore(secret_provider=lambda: "task-test-only").load(
            owner=OWNER, session_id="native-recommend-evidence")
        assert [fact["query_key"] for fact in cold.metadata["task"]["verified_facts"]] == [
            fact["query_key"] for fact in facts]

    asyncio.run(run())
