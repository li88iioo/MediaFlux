"""原生任务边界：用受控RPC时序复用真实发布事务，不靠sleep竞态。"""
import asyncio
import json

from app.agent.kernel.bootstrap import build_agent_kernel_runtime
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.state_types import AgentInput
from app.agent.kernel.turn import AgentTurn
from app.agent.owner_routes import web_kernel_owner
from tests import test_agent_session_result as native_tests
from tests import test_agent_task_cas as cas_tests
from tests.test_agent_kernel_core import ScriptedModel
from tests.test_agent_task_first_slice import GOAL, OWNER, declaration
from tests.test_agent_task_native import code_round, final

native_workspace = native_tests.native_workspace
task_store = cas_tests.task_store

def completed(events):
    result = next(e.payload['result']['data'] for e in events if e.type.value == 'tool.completed')
    assert result['exit_code'] == 0, result['stderr']
    return json.loads(result['stdout'])


def test_concurrent_revisions_use_original_cas(task_store, native_workspace, monkeypatch):
    real_control = AgentTurn.control_task
    async def run():
        ready = asyncio.Event()
        arrived = 0
        async def gated(turn, patch):
            nonlocal arrived
            if patch is not None and patch.operation == 'revise':
                arrived += 1
                if arrived == 2:
                    ready.set()
                await asyncio.wait_for(ready.wait(), 5)
            return await real_control(turn, patch)
        monkeypatch.setattr(AgentTurn, 'control_task', gated)
        args = declaration()
        args.pop('operation')
        script = f'''import json
from concurrent.futures import ThreadPoolExecutor
from mf import session, Error
t = session.start_task(**{args!r})['data']['task']
# 只传原声明字段；投影中的state/evidence_ids不能作为义务输入。
original = {args!r}
def revise(label):
    try:
        return session.revise_task(original['root_goal'], [label], original['obligations'], source=original['source'])
    except Error as exc:
        return {{'code': exc.code}}
with ThreadPoolExecutor(2) as pool:
    outcomes = list(pool.map(revise, ['first', 'second']))
assert sum(v.get('code') == 'task_conflict' for v in outcomes) == 1, outcomes
current = session.task()['data']['task']
assert current['task_id'] == t['task_id'] and current['epoch'] == 2 and current['revision'] == 2
assert current['constraints'] in [['first'], ['second']]
print(json.dumps(current))'''
        runtime = build_agent_kernel_runtime(model=ScriptedModel([code_round(script), final()]), store=task_store)
        events = [e async for e in runtime.session.run(AgentInput(message=GOAL, owner=OWNER, session_id='native-cas'))]
        task = completed(events)
        saved = await SQLiteKernelStore(secret_provider=lambda: 'task-test-only').load(owner=OWNER, session_id='native-cas')
        assert arrived == 2 and saved.metadata['task']['task_id'] == task['task_id']
        assert saved.metadata['task']['revision'] == 2
    asyncio.run(run())


def test_forged_evidence_and_identity_rejected_cancel_only_tracking(task_store, native_workspace):
    args = declaration()
    args.pop('operation')
    script = f'''import json
from mf import session, Error
t = session.start_task(**{args!r})['data']['task']
try:
    session.checkpoint_task(['invented-evidence'], [])
except Error as exc:
    assert exc.code == 'task_conflict'
else:
    raise AssertionError('forged evidence accepted')
assert session.task()['data']['task'] == t
try:
    session.cancel_task(source={GOAL!r}, owner='another-owner')
except TypeError:
    pass
else:
    raise AssertionError('caller-supplied identity accepted')
before = session.pending_plan()
session.cancel_task(source={GOAL!r})
try:
    session.cancel_task(source={GOAL!r})
except Error as exc:
    assert exc.code == 'task_conflict'
else:
    raise AssertionError('cancelled task was treated as active')
assert session.pending_plan() == before
t = session.task()['data']['task']
assert t['status'] == 'cancelled' and t['verified_facts'] == []
print(json.dumps(t))'''
    async def run():
        runtime = build_agent_kernel_runtime(model=ScriptedModel([code_round(script), final()]), store=task_store)
        events = [e async for e in runtime.session.run(AgentInput(message=GOAL, owner=OWNER, session_id='native-cancel'))]
        task = completed(events)
        assert task['status'] == 'cancelled' and task['constraints'] == args['constraints']
        for owner, sid in [(web_kernel_owner('other'), 'native-cancel'), (OWNER, 'another-session')]:
            other = build_agent_kernel_runtime(model=ScriptedModel([code_round(
                "import json\nfrom mf import session\nprint(json.dumps(session.task()))"), final()]), store=task_store)
            read = [e async for e in other.session.run(AgentInput(message='读取当前任务', owner=owner, session_id=sid))]
            assert completed(read)['status'] == 'empty'
        original = await task_store.load(owner=OWNER, session_id='native-cancel')
        assert original.metadata['task']['task_id'] == task['task_id']
    asyncio.run(run())
