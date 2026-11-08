"""Historical reference projection must not block unrelated admitted turns."""
import asyncio
import json
import time
from types import SimpleNamespace as NS
from unittest.mock import patch

import pytest

from app.agent.errors import ToolPipelineError
from app.agent.kernel import model_rounds as model_rounds_module
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.projection import ToolOutcome
from app.agent.kernel.session import SessionLimits
from app.agent.kernel.session_service import SessionService
from app.agent.kernel.state_types import AgentInput, CancellationToken, StateUpdate
from app.agent.kernel.tool_calls import ToolCallContext
from tests import test_agent_kernel_telegram_reference as o
from tests.support import isolated_test_database
from tests.test_agent_argument_fidelity import declaration, text_reply
from tests.test_agent_capacity_recovery import (
    OWNER,
    SESSION,
    assert_unknown,
    batch,
    collect,
    make_wire,
    reopened,
    result_rows,
    session,
)

store = o.store
native_workspace = o.native_workspace


def send(store, message, window=128000):
    wire = o.Wire()
    wire.reading = True
    runtime = o.runtime(store, wire)
    runtime.telegram.session.limits = SessionLimits(context_window_tokens=window, max_output_tokens=6000)
    with patch.object(o.adapter, 'get_agent_kernel_runtime', return_value=runtime):
        result = o.adapter._execute_query(o.FakeBot(), o.TELEBOT, message,
            chat_id=str(message.chat.id), user_id=str(message.from_user.id), text=message.text)
    return result, wire


@pytest.mark.parametrize('elapsed', [0, 901])
def test_unrelated_turn_after_reader_roundtrip_and_expiry(store, elapsed):
    first, _, owner, sid = o.ingress(store)
    assert first.status == 'success'
    before = asyncio.run(store.load(owner=owner, session_id=sid)).conversation[0]['reply_context']
    cold = o.SQLiteKernelStore(secret_provider=lambda: 'id08-offline-key', clock=lambda: time.time() + elapsed)
    result, wire = send(cold, o.Message('新问题：你好', chat_id=-876543, user_id=73, message_id=101))
    assert result.status == 'success', (result.status, result.error_code, len(wire.requests))
    assert len(wire.requests) == 1
    after = asyncio.run(cold.load(owner=owner, session_id=sid)).conversation[0]['reply_context']
    assert after == before
    users = [m['content'] for m in wire.requests[0]['messages'] if m['role'] == 'user']
    if elapsed:
        assert 'reference_unavailable' in users[0]
        assert 'untrusted_reference' in users[0] and 'source' in users[0]
        assert before['reference_handle'] not in users[0]
        assert 'read_method' not in users[0]
        # 原工作区输出是已读事实；过期只撤回引用入口，不覆写持久 stdout。
        old_tools = [m for m in wire.requests[0]['messages'] if m['role'] == 'tool']
        assert old_tools
        assert before['text'][2000:] in o.reader_content(wire.requests[0], 'chat_completions')['data']
    async def read():
        lease, _ = await cold.begin_turn(owner=owner, session_id=sid, request_id='expired-reader')
        async def progress(payload):
            pass
        context = ToolCallContext(owner=owner, session_id=sid, request_id='expired-reader',
            turn_id=lease.turn_id, lease=lease, cancellation=CancellationToken(), report_progress=progress)
        return await SessionService(ToolPipeline(catalog=ToolCatalog([]), state_store=cold, reference_store=cold), context).result(before['reference_handle'], '/text')
    if elapsed:
        with pytest.raises(ToolPipelineError) as error:
            asyncio.run(read())
        assert error.value.code == 'reference_invalid'
    else:
        assert asyncio.run(read())['data'] == o.LONG


@pytest.mark.parametrize('window', [128000, 16384])
def test_normal_old_group_pruning_does_not_freeze_new_turn(store, window):
    first = o.Message('旧任务背景' + '文' * 11000, chat_id=-987654, user_id=92, message_id=100)
    first.reply_to_message = NS(text='字' * 2100, caption='', message_id=89, chat=first.chat)
    assert send(store, first)[0].status == 'success'
    assert send(store, o.Message('最近独立任务：你好', chat_id=-987654, user_id=92, message_id=101))[0].status == 'success'
    observed = []
    original = model_rounds_module.bounded_model_messages
    def observe(*args, **kwargs):
        result = original(*args, **kwargs)
        observed.append(result)
        return result
    with patch.object(model_rounds_module, 'bounded_model_messages', observe):
        result, wire = send(store, o.Message('新的普通问题：你好', chat_id=-987654, user_id=92, message_id=102), window)
    assert observed
    if window == 16384:
        assert all('旧任务背景' not in m.content for m in observed[0])
        assert any('最近独立任务' in m.content for m in observed[0])
    assert result.status == 'success', (result.status, result.error_code, len(wire.requests))
    assert len(wire.requests) == 1


@pytest.mark.parametrize('reference', [False, True])
def test_late_unknown_capacity_failure_preserves_publication_facts(reference):
    async def scenario():
        handled = []
        def read(arguments, context):
            handled.append(arguments)
            return ToolOutcome(model_content='{}', public_content={'ok': True, 'status': 'success'},
                state_updates=(StateUpdate('metadata.pad', 'p' * 270000),))
        catalog = ToolCatalog([declaration(read)])
        original = make_wire('chat_completions', [batch('chat_completions', [('unknown-old', 'inspect_args', {'selector': 'original'})])])
        await collect(session(catalog, original), 'original')
        assert_unknown(result_rows(await reopened())[0])
        assert handled == [{'selector': 'original'}]
        handled.clear()
        wire = make_wire('chat_completions', [text_reply('chat_completions')])
        source = {'kind': 'text', 'chat_id': '-876543', 'message_id': 90, 'thread_id': None,
                  'incoming_message_id': 100, 'incoming_thread_id': None}
        events = [e async for e in session(catalog, wire).run(AgentInput(owner=OWNER, session_id=SESSION,
            request_id='new', message='Explain the previous unknown result; do not execute.',
            reply_context={'text': 'new reference ' + 'Z' * 2200, 'source': source} if reference else {}))]
        cold = await reopened()
        assert not handled and len(wire.requests) == 1
        assert_unknown(next(r for r in result_rows(cold) if r.get('tool_call_id') == 'unknown-old'))
        assert 'unknown-old' in json.dumps(wire.requests) and 'result_unknown' in json.dumps(wire.requests)
        assert events[-1].payload['code'] == 'internal_error'
        assert '未发送给模型' not in events[-1].payload.get('message', '')
        if reference:
            assert any(r.get('reply_context', {}).get('text') == 'new reference ' + 'Z' * 2200 for r in cold.conversation)
    with isolated_test_database():
        asyncio.run(scenario())
