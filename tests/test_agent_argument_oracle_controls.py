"""Narrow proposed oracle migrations, never edits to existing tests."""
import asyncio
import json
from dataclasses import replace

import pytest

from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.session import SessionLimits
from tests.support import agent_llm_enabled as agent_llm_enabled  # noqa: PLC0414
from tests.support import isolated_test_database
from tests.test_agent_capacity_recovery import (
    assert_native_wire,
    batch,
    collect,
    make_wire,
    reopened,
    result_rows,
    session,
    spec,
)
from tests.test_agent_provider_state import SIGNATURE
from tests.test_agent_provider_text_authority import native_reply

P='anthropic_messages'


def catalog_for(handled):
    def handler(a,c):
        handled.append(dict(a)); return {'ok':True,'data':{'fact':'FIRST_FACT'}}
    return ToolCatalog([replace(spec('inspect_args',handler),input_schema={'type':'object','additionalProperties':True})])

@pytest.mark.parametrize('prefix',[False,True])
def test_native_capacity_rejection_does_not_execute_or_erase_prefix(agent_llm_enabled, prefix):
    async def run():
        handled=[]; cat=catalog_for(handled)
        prior=[('prior','inspect_args',{'ordinal':0})] if prefix else []
        rejected=[('big','inspect_args',{'ordinal':1})]
        big=batch(P,rejected)
        next(b for b in big['content'] if b['type']=='thinking')['signature']=SIGNATURE*2500
        wire=make_wire(P,([batch(P,prior)] if prefix else [])+[big])
        events=await collect(session(cat,wire,limits=SessionLimits(context_window_tokens=16384)))
        state=await reopened()
        cold=make_wire(P,[native_reply(P,'已核对',private=False)])
        await collect(session(cat,cold,limits=SessionLimits(context_window_tokens=16384)),'cold')
        assert handled==[c[2] for c in prior]
        assert len(wire.requests)==(2 if prefix else 1)
        assert 'context_budget_exceeded' in repr(events)
        assert [(c['call_id'],c['name'],c['arguments']) for r in state.conversation for c in r.get('tool_calls',[])]==prior
        if prefix:
            assert_native_wire(P,cold.requests[0],prior)
            assert 'FIRST_FACT' in result_rows(state)[0]['content']
        else:
            assert SIGNATURE not in json.dumps(cold.requests[0])
    with isolated_test_database(): asyncio.run(run())


def test_large_native_signature_is_exact_when_admitted_and_old_group_drops_whole(agent_llm_enabled):
    async def run():
        from app.agent.kernel.conversation_state import retain_conversation
        handled=[]; cat=catalog_for(handled)
        calls=[('large-admitted','inspect_args',{'ordinal':0})]
        reply=batch(P,calls); signature=SIGNATURE*2500
        next(b for b in reply['content'] if b['type']=='thinking')['signature']=signature
        wire=make_wire(P,[reply,native_reply(P,'已核对',private=False)])
        events=await collect(session(cat,wire))
        state=await reopened()
        assert events[-1].payload.get('status')=='success'
        assert handled==[calls[0][2]]
        row=next(r for r in state.conversation if r.get('provider_state'))
        assert json.loads(row['provider_state']['layout_json'])[0]['signature']==signature
        kept=retain_conversation(state.conversation,maximum_bytes=2048)
        assert not any(r.get('provider_state') or r.get('role')=='tool' for r in kept)
    with isolated_test_database(): asyncio.run(run())


def test_duplicate_native_batch_rejected_before_append_preserves_valid_pair(agent_llm_enabled):
    async def run():
        handled=[]; cat=catalog_for(handled)
        prior=[('duplicate','inspect_args',{'ordinal':0})]
        rejected=[('duplicate','inspect_args',{'ordinal':1})]
        wire=make_wire(P,[batch(P,prior),batch(P,rejected)])
        events=await collect(session(cat,wire))
        state=await reopened()
        cold=make_wire(P,[native_reply(P,'已核对',private=False)])
        await collect(session(cat,cold),'cold')
        assert 'provider_state_conflict' in repr(events)
        assert len(wire.requests)==2
        assert handled==[prior[0][2]]
        assert [(c['call_id'],c['name'],c['arguments']) for r in state.conversation for c in r.get('tool_calls',[])]==prior
        assert 'FIRST_FACT' in result_rows(state)[0]['content']
        assert_native_wire(P,cold.requests[0],prior)
        assert handled==[prior[0][2]]
    with isolated_test_database(): asyncio.run(run())
