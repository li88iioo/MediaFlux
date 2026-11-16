"""Positive current-execution retention at the real receipt merge boundary."""
import asyncio
import json
from copy import deepcopy
from dataclasses import replace

import pytest

from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.projection import ToolOutcome
from app.agent.kernel.state import InMemorySessionStateStore
from app.agent.kernel.state_types import StateUpdate
from tests.support import agent_llm_enabled as agent_llm_enabled  # noqa: PLC0414
from tests.support import isolated_test_database
from tests.test_agent_capacity_recovery import (
    OWNER,
    PROTOCOLS,
    SESSION,
    batch,
    collect,
    make_wire,
    reopened,
    result_rows,
    session,
    spec,
)
from tests.test_agent_provider_text_authority import native_reply


def ids(state):
    return [c['call_id'] for r in state.conversation for c in r.get('tool_calls',[])]

@pytest.mark.parametrize('store_kind',['sqlite','memory'])
@pytest.mark.parametrize('protocol',PROTOCOLS)
@pytest.mark.parametrize('mode',['commit_boundary','callback','state_updates'])
@pytest.mark.parametrize('pressure',[False,True])
def test_receipt_merge_preserves_executed_call_group(agent_llm_enabled, protocol,mode,pressure,store_kind):
    async def exercise():
        calls=[(f'work-{i}','inspect_args',{'literal':chr(65+i)*31000,'ordinal':i}) for i in range(5)]
        receipts={f'legal-plan-{i}':{'state':'terminal','delivered':False,'receipt_message':{
            'role':'assistant','content':'可信完成回执：'+('R'*7500 if pressure else 'done'),
            'tool_name':'old.inspect','effect_plan_id':f'legal-plan-{i}','completion_receipt_id':f'legal-plan-{i}'}}
            for i in range(2)}
        handled=[]; snapshots=[]; injected=False
        store = SQLiteKernelStore(secret_provider=lambda: 'started-checkpoint-synthetic') if store_kind == 'sqlite' else InMemorySessionStateStore()
        original_commit = store.commit

        async def commit(lease, *, conversation=None, updates=(), task_patch=None):
            nonlocal injected
            complete = conversation is not None and any(r.get('tool_call_id') == 'work-4' and 'FACT-4' in str(r.get('content')) for r in conversation)
            if mode == 'commit_boundary' and complete and not injected:
                injected = True
                await store.update_effect_state(owner=OWNER, session_id=SESSION, change=lambda state: state.metadata.update(effect_waits=deepcopy(receipts)))
            return await original_commit(lease, conversation=conversation, updates=updates, task_patch=task_patch)

        store.commit = commit
        async def load():
            return await reopened() if store_kind=='sqlite' else await store.load(owner=OWNER,session_id=SESSION)
        async def handler(value,context):
            snap=await load(); snapshots.append(snap)
            handled.append(value['ordinal'])
            assert ids(snap)==[c[0] for c in calls]
            assert [(c['call_id'],c['name'],c['arguments']) for r in snap.conversation for c in r.get('tool_calls',[])]==calls
            assert [r.get('provider_state') for r in snap.conversation if r.get('tool_calls')]==[r.get('provider_state') for r in snapshots[0].conversation if r.get('tool_calls')]
            assert json.loads(result_rows(snap)[value['ordinal']]['content'])['code']=='result_unknown'
            for prior in range(value['ordinal']):
                assert 'FACT-'+str(prior) in result_rows(snap)[prior]['content']
            updates=()
            if value['ordinal']==4:
                if mode=='callback':
                    await store.update_effect_state(owner=OWNER,session_id=SESSION,change=lambda s:s.metadata.update(effect_waits=deepcopy(receipts)))
                if mode=='state_updates':
                    updates=(StateUpdate('metadata.effect_waits',deepcopy(receipts)),)
            return ToolOutcome(model_content=json.dumps({'ok':True,'data':{'fact':'FACT-'+str(value['ordinal'])}}),public_content={'ok':True,'status':'success','summary':'observed'},state_updates=updates)
        catalog=ToolCatalog([replace(spec('inspect_args',handler),input_schema={'type':'object','additionalProperties':True})])
        wire=make_wire(protocol,[batch(protocol,calls),native_reply(protocol,'已核对',private=False)])
        active=session(catalog,wire,state_store=store)
        if store_kind=='memory':
            active.journal=None
        events=await collect(active)
        state=await load()
        assert handled==list(range(5)), 'fixture must execute the real handler after durable started'
        # Either complete results or the last started checkpoint must survive.
        assert ids(state)==[c[0] for c in calls], 'receipt merge must not erase the current executed group'
        assert [r.get('provider_state') for r in state.conversation if r.get('tool_calls')]==[r.get('provider_state') for r in snapshots[0].conversation if r.get('tool_calls')]
        for prior in range(4): assert 'FACT-'+str(prior) in result_rows(state)[prior]['content']
        assert [(c['call_id'],c['name'],c['arguments']) for r in state.conversation for c in r.get('tool_calls',[])]==calls
        assert state.metadata['effect_waits']==receipts
        if pressure:
            assert 'history_capacity_exceeded' in repr(events)
            assert len(wire.requests)==1
            assert state.conversation==snapshots[-1].conversation
            assert state.execution_capacity is True
        else:
            assert events[-1].payload.get('status')=='success'
            assert len(wire.requests)==2
            assert 'FACT-4' in result_rows(state)[4]['content']
        assert handled==list(range(5))
        if 'FACT-4' not in result_rows(state)[4]['content']:
            assert json.loads(result_rows(state)[4]['content'])['code']=='result_unknown'
            assert events[-1].payload.get('status')!='success'
    with isolated_test_database(): asyncio.run(exercise())
