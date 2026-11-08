import asyncio
import json

import pytest

from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.model import ModelEventType
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.session import AgentSession
from app.agent.kernel.state_types import AgentInput
from tests.test_agent_tool_argument_ingress import (
 PROTOCOLS,
 Wire,
 encode,
 event_wire,
 frames,
 observe,
 wire,
)
from tests.test_agent_tool_argument_ingress import offline as offline
from tests.test_agent_tool_argument_ingress import store as store

RAW='{"query": "example",'
CASES=[('chat_completions','json'),('chat_completions','sse'),('responses','json'),('responses','sse')]

def reply(protocol,mode,malformed):
 value=RAW if malformed else '{"query":"example"}'
 if protocol=='anthropic_messages':
  if mode=='sse': return event_wire(frames(protocol,{},[value]))
  return wire(protocol,mode,{'query':'example'})
 return wire(protocol,mode,value)

@pytest.mark.parametrize('protocol,mode',CASES)
def test_identifiable_malformed_call_is_repairable_not_provider_fault(protocol,mode):
 events,error=asyncio.run(observe(protocol,reply(protocol,mode,True)))
 assert error is None, 'Identified malformed JSON must reach bounded validation repair, not Provider fault'
 calls=[e.tool_call for e in events if e.tool_call]
 assert len(calls)==1 and calls[0].call_id=='call-0' and calls[0].name=='lookup'
 assert events[-1].type is ModelEventType.FINISH

@pytest.mark.parametrize('protocol',PROTOCOLS)
@pytest.mark.parametrize('mode',['json','sse'])
def test_native_valid_control(protocol,mode):
 events,error=asyncio.run(observe(protocol,reply(protocol,mode,False)))
 assert error is None
 actual_calls = []
 for e in events:
     if e.tool_call:
         assert e.tool_call.arguments is not None
         actual_calls.append(dict(e.tool_call.arguments))
 assert actual_calls==[{'query':'example'}]
 assert events[-1].type is ModelEventType.FINISH

@pytest.mark.parametrize('mode',['json','sse'])
def test_broken_outer_remains_provider_fault(mode):
 body=b'{broken' if mode=='json' else b'data: {broken\n\n'
 events,error=asyncio.run(observe('responses',(body,'application/json' if mode=='json' else 'text/event-stream')))
 assert error is not None
 assert not [e for e in events if e.tool_call]

def test_real_session_malformed_then_correction_reaches_model(store):
 async def run():
  executed=[]
  def handler(arguments,context):
   executed.append(dict(arguments)); return {'ok':True,'status':'success','summary':'已核验','data':{}}
  catalog=ToolCatalog([KernelToolSpec(name='lookup',domain='library',description='只读核验',input_schema={'type':'object','properties':{'query':{'type':'string'}},'required':['query']},effect=ToolEffect.READ,read=handler)])
  final={'choices':[{'message':{'content':'已核验完成'},'finish_reason':'stop'}]}
  corrected, kind = reply('chat_completions','sse',False)
  transport=Wire('chat_completions',[reply('chat_completions','sse',True),(corrected.replace(b'call-0', b'call-corrected'),kind),(encode(final).encode(),'application/json')])
  session=AgentSession(model=transport.adapter,catalog=catalog,pipeline=ToolPipeline(catalog=catalog,state_store=store),state_store=store)
  events=[e async for e in session.run(AgentInput(message='只读核验',owner='owner',session_id='id26'))]
  saved=await SQLiteKernelStore(secret_provider=lambda:'offline-tool-argument-test').load(owner='owner',session_id='id26')
  assert events and saved.conversation
  assert all(c._client.is_closed for c in transport.clients) and all(b.closed for b in transport.bodies)
  assert len(transport.requests)==3, 'Malformed identifiable call must produce structured validation feedback and allow one corrected call'
  assert executed==[{'query':'example'}]
  feedback=[m for m in transport.requests[1]['messages'] if m.get('role')=='tool']
  assert feedback and json.loads(feedback[-1]['content'])['code']=='invalid_arguments'
 asyncio.run(run())
