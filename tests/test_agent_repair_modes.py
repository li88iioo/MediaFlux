"""原工具直接重试允许合法模式切换，仍须预检和授权。"""
from __future__ import annotations
from typing import Any

import asyncio
import json
from copy import deepcopy

import pytest

from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.tool_calls import PreparedEffect
from app.agent.kernel.events import AgentEventType
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.session import AgentSession
from app.agent.kernel.state_types import AgentInput
from tests.test_agent_recovered_progress import reply
from tests.test_agent_tool_argument_ingress import Wire
from tests.test_agent_tool_argument_ingress import offline as offline
from tests.test_agent_tool_argument_ingress import store as store

_PROTOCOL = 'anthropic_messages'
_OWNER = 'repair-modes-owner'
_SESSION = 'repair-modes-session'
_FINAL = '已按真实工具回执完成核验。'


def _session(model, catalog, store):
    return AgentSession(model=model, catalog=catalog,
                        pipeline=ToolPipeline(catalog=catalog, state_store=store), state_store=store)


async def _run(catalog, store, name, arguments):
    replies = [reply(_PROTOCOL, name=catalog.get(name).model_name, arguments=value, index=index)
               for index, value in enumerate(arguments)] + [reply(_PROTOCOL, text=_FINAL)]
    wire = Wire(_PROTOCOL, replies)
    events = [event async for event in _session(wire.adapter, catalog, store).run(
        AgentInput(message='核验媒体库与规则能力', owner=_OWNER, session_id=_SESSION))]
    cold = SQLiteKernelStore(secret_provider=lambda: 'offline-tool-argument-test')
    saved = await cold.load(owner=_OWNER, session_id=_SESSION)
    feedback = [json.loads(row['content']) for row in saved.conversation if row.get('role') == 'tool']
    return events, feedback, saved, wire




_IDENTITY_SCHEMA = {
    'type': 'object', 'required': ['id'],
    'properties': {'id': {'type': 'string', 'minLength': 1},
                   'filter': {'type': 'string', 'minLength': 1},
                   'limit': {'type': 'integer', 'minimum': 1}},
    'additionalProperties': False,
}
_WRITE_SCHEMA = {
    'type': 'object', 'required': ['id'],
    'properties': {'id': {'type': 'string', 'minLength': 1},
                   'period': {'type': 'integer', 'minimum': 1}},
    'additionalProperties': False,
}


def _read_catalog(schema, executed):
    def read(arguments, context):
        executed.append(deepcopy(arguments))
        return {'ok': True, 'status': 'success', 'summary': '真实只读 handler 已完成', 'data': {}}
    return ToolCatalog([KernelToolSpec(name='library.lookup', domain='library', description='读取媒体库范围',
        input_schema=deepcopy(schema), effect=ToolEffect.READ, read=read)])


def _write_catalog(previews, writes, *, schema=None, authorize=None, effect=ToolEffect.WRITE):
    def prepare(arguments, context):
        previews.append(deepcopy(arguments))
        return PreparedEffect(preview={'summary': '规则预览，尚未执行'}, snapshot_fingerprint='offline-rule-snapshot')
    def write(arguments, snapshot_fingerprint, context):
        writes.append(deepcopy(arguments))
        return {'ok': True, 'status': 'success', 'summary': '规则写入完成'}
    return ToolCatalog([KernelToolSpec(name='rule.create', domain='rule', description='创建媒体库规则',
        input_schema=deepcopy(_WRITE_SCHEMA if schema is None else schema), effect=effect,
        validator=lambda arguments: {'period': 10080, **arguments}, authorize=authorize,
        prepare=prepare, execute_confirmed=write)])


def test_ordinary_read_max_properties_modes_are_independent_without_name_exception(store):
    async def run():
        executed = []
        schema: dict[str, Any] = {'type': 'object', 'maxProperties': 1, 'additionalProperties': False, 'properties': {
            'listing': {'type': 'object', 'properties': {}, 'additionalProperties': False},
            'search': {'type': 'string', 'minLength': 1},
        }}
        catalog = _read_catalog(schema, executed)
        bad, independent = {'listing': {'_错误': '未知字段'}}, {'search': '媒体库'}
        events, feedback, _, wire = await _run(catalog, store, 'library.lookup', [bad, independent])
        wire.verify(3)
        assert executed == [independent], 'schema modes must work for ordinary READ, not a capabilities-name exemption'
        assert feedback[0]['code'] == 'invalid_arguments' and feedback[1]['status'] == 'success'
        assert events[-1].payload['status'] == 'success'
    asyncio.run(run())


@pytest.mark.parametrize('corrected', [{'id': 'A', 'limit': 3}, {'id': 'B'}],
                         ids=['drop-optional', 'change-required-selector'])
def test_read_can_change_selector_and_drop_optional_filter(corrected, store):
    async def run():
        executed = []
        catalog = _read_catalog(_IDENTITY_SCHEMA, executed)
        bad = {'id': 'A', 'filter': 'unwatched-only', 'limit': 'invalid'}
        events, feedback, saved, wire = await _run(catalog, store, 'library.lookup', [bad, corrected])
        wire.verify(3)
        assert executed == [corrected]
        assert feedback[0]['code'] == 'invalid_arguments' and feedback[1]['ok'] is True
        assert events[-1].payload['status'] == 'success'
        calls = [call for row in saved.conversation for call in row.get('tool_calls', [])]
        assert [call['arguments'] for call in calls] == [bad, corrected]
    asyncio.run(run())


@pytest.mark.parametrize('effect', [ToolEffect.WRITE, ToolEffect.DANGER])
@pytest.mark.parametrize('period', ['invalid', False, 0, []], ids=['string', 'false', 'zero', 'empty-list'])
def test_invalid_optional_period_can_be_omitted_but_only_creates_preview(period, effect, store):
    async def run():
        previews, writes = [], []
        catalog = _write_catalog(previews, writes, effect=effect)
        bad, omitted = {'id': 'A', 'period': period}, {'id': 'B'}
        events, feedback, saved, wire = await _run(catalog, store, 'rule.create', [bad, omitted])
        wire.verify(2)
        assert feedback[0]['code'] == 'invalid_arguments'
        assert feedback[0]['expected_input_schema'] == _WRITE_SCHEMA
        assert previews == [{'id': 'B', 'period': 10080}] and writes == []
        assert saved.pending_effect_plan_id == events[-1].payload['plan_id']
        assert events[-1].payload['status'] == 'approval_required'
        assert any(event.type is AgentEventType.EFFECT_APPROVAL_REQUIRED for event in events)
        assert not any(event.type is AgentEventType.EFFECT_COMPLETED for event in events)
    asyncio.run(run())


@pytest.mark.parametrize('composition', ['not', 'oneOf'])
@pytest.mark.parametrize('replacement', [{}, {'period': 60}], ids=['drop-both', 'keep-one'])
def test_direct_write_retry_uses_current_mutual_exclusion_schema(composition, replacement, store):
    async def run():
        previews, writes = [], []
        schema: dict[str, Any] = deepcopy(_WRITE_SCHEMA)
        schema['properties']['schedule'] = {'type': 'string', 'minLength': 1}
        schema.update({'not': {'required': ['period', 'schedule']}} if composition == 'not' else {
            'oneOf': [{'required': ['period']}, {'required': ['schedule']}]})
        catalog = _write_catalog(previews, writes, schema=schema)
        bad, corrected = {'id': 'A', 'period': 60, 'schedule': 'daily'}, {'id': 'A', **replacement}
        events, feedback, saved, wire = await _run(catalog, store, 'rule.create', [bad, corrected])
        assert feedback[0]['code'] == 'invalid_arguments' and writes == []
        if composition == 'not' or replacement:
            wire.verify(2)
            assert previews == [{'period': 10080, **corrected}]
            assert saved.pending_effect_plan_id == events[-1].payload['plan_id']
            assert events[-1].payload['status'] == 'approval_required'
            assert not any(event.type is AgentEventType.EFFECT_COMPLETED for event in events)
        else:
            wire.verify(3)
            assert previews == [] and not saved.pending_effect_plan_id
            assert feedback[1]['code'] == 'invalid_arguments'
            assert events[-1].payload.get('status') != 'success'
    asyncio.run(run())


@pytest.mark.parametrize('effect', [ToolEffect.WRITE, ToolEffect.DANGER])
def test_direct_retry_does_not_bypass_revoked_write_permission(store, effect):
    async def run():
        previews, writes = [], []
        allowed = [True]
        catalog = _write_catalog(previews, writes, authorize=lambda context: allowed[0], effect=effect)
        bad, corrected = {'id': 'A', 'period': 'invalid'}, {'id': 'B', 'period': 60}
        replies = [reply(_PROTOCOL, name=catalog.get('rule.create').model_name, arguments=value, index=index)
                   for index, value in enumerate([bad, corrected])] + [reply(_PROTOCOL, text='没有权限，未执行写入。')]
        wire = Wire(_PROTOCOL, replies)
        original_handler = wire.handler

        async def revoke_before_retry(request):
            if len(wire.requests) == 1:
                allowed[0] = False
            return await original_handler(request)

        wire.handler = revoke_before_retry
        events = [event async for event in _session(wire.adapter, catalog, store).run(
            AgentInput(message='创建媒体规则', owner=_OWNER, session_id=_SESSION))]
        wire.verify(3)
        saved = await SQLiteKernelStore(secret_provider=lambda: 'offline-tool-argument-test').load(
            owner=_OWNER, session_id=_SESSION)
        feedback = [json.loads(row['content']) for row in saved.conversation if row.get('role') == 'tool']
        assert feedback[0]['code'] == 'invalid_arguments'
        assert feedback[1]['code'] == 'authorization_denied'
        assert previews == [] and writes == [] and not saved.pending_effect_plan_id
        assert events[-1].payload.get('status') not in {'success', 'approval_required'}
    asyncio.run(run())


@pytest.mark.parametrize('shape', ['multiple-modes', 'single-filter-property'])
def test_empty_read_retry_uses_current_schema_not_old_fields(shape, store):
    async def run():
        executed = []
        if shape == 'multiple-modes':
            properties = {'listing': {'type': 'object', 'properties': {}, 'additionalProperties': False},
                          'search': {'type': 'string', 'minLength': 1}}
            bad = {'listing': {'_错误': '未知字段'}}
        else:
            properties = {'filter': {'type': 'string', 'minLength': 1}}
            bad = {'filter': 'unwatched-only', '_错误': '未知字段'}
        schema: dict[str, Any] = {'type': 'object', 'properties': properties, 'additionalProperties': False, 'maxProperties': 1}
        catalog = _read_catalog(schema, executed)
        events, feedback, saved, wire = await _run(catalog, store, 'library.lookup', [bad, {}])
        wire.verify(3)
        assert executed == [{}], 'the current schema permits an empty READ request'
        assert feedback[0]['code'] == 'invalid_arguments'
        assert feedback[1]['ok'] is True
        assert events[-1].payload['status'] == 'success'
        calls = [call for row in saved.conversation for call in row.get('tool_calls', [])]
        assert [call['arguments'] for call in calls] == [bad, {}]
    asyncio.run(run())
