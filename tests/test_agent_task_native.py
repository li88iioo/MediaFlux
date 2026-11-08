"""原生会话任务：真实root工作区、SQLite发布和原验收证据。"""
import asyncio
import json

from app import sdk
from app.agent.feature_gate import initialize_runtime_fence
from app.agent.kernel.bootstrap import (
    _capture_creation_binding,
    build_agent_kernel_runtime,
)
from app.agent.kernel.model import ModelEvent, ModelEventType, ModelToolCall
from app.agent.kernel.state_types import AgentInput
from tests import test_agent_session_result as native_tests
from tests import test_agent_task_cas as cas_tests
from tests.test_agent_kernel_core import ScriptedModel
from tests.test_agent_task_first_slice import GOAL, OWNER, declaration

native_workspace = native_tests.native_workspace
task_store = cas_tests.task_store

def code_round(code, *, language="python"):
    return [ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall(
        "native-task", "agent.run_code", {"code": code, "language": language})),
        ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")]


def final():
    return [ModelEvent(ModelEventType.TEXT_DELTA, text="已记录真实核验。"),
            ModelEvent(ModelEventType.FINISH, finish_reason="stop")]


def test_same_script_start_read_checkpoint_and_cli(task_store, native_workspace, monkeypatch):
    real_calls = []
    def component(component):
        real_calls.append(component)
        return {"ok": True, "status": "not_configured", "data": {
            "component": component, "status": "not_configured", "enabled": False,
            "probe_mode": "configuration_only", "network_accessed": False}}
    monkeypatch.setitem(sdk.METHODS, "diagnostics.component", component)
    args = declaration()
    args.pop("operation")
    code = f'''import json
from mf import session, diagnostics
t = session.start_task(**{args!r})['data']['task']
assert t['revision'] == 1 and t['status'] == 'partial'
assert session.task()['data']['task'] == t
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
        assert real_calls == ["tmdb", "qbittorrent"]
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
