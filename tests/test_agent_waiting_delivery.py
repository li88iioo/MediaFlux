"""READ 等待续行复用 effect 的终态/outbox，不运行第二套发送器。"""
import asyncio
import json
from dataclasses import replace
from datetime import datetime
from unittest.mock import patch

import httpx
import pytest

from app import config
from app import database as db
from app.agent.kernel.bootstrap import (
    _capture_creation_binding,
)
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.model_catalog import provider_channel_fingerprint
from app.agent.transports import QueryEnvelope
from app.modules.agent_jobs_scheduler import AgentJobsScheduler
from tests.agent_task_native_helpers import task_function
from tests.test_agent_read_wait_contract_repair import resumed
from tests.test_agent_read_wait_entry_exclusions import tg_config as tg_config
from tests.test_agent_read_wait_lifecycle import (
    GOAL,
    Quota,
    WaitScript,
    build_wait_runtime,
)
from tests.test_agent_read_wait_lifecycle import (
    isolated_database as isolated_database,
)

OWNER, SID = 'tg:v1:-100123\x1f7', 'read-wait-delivery'
ROUTE = {'chat_id': '-100123', 'message_thread_id': 42}


async def park(clock, route):
    config.set_and_save({'TG_AGENT_ENABLED': '1', 'TG_CHAT_ID': '-100123', 'TG_AGENT_ALLOWED_USER_IDS': '7'})
    script = WaitScript()
    script.adapter.settings = replace(script.adapter.settings, telegram_channel=provider_channel_fingerprint(config.get_many(("AGENT_LLM_API_URL", "AGENT_LLM_PROTOCOL", "AGENT_LLM_API_KEY"))))
    runtime = build_wait_runtime(model=script.adapter, store=SQLiteKernelStore(clock=lambda: clock[0]))
    runtime.session.continuation_binding = _capture_creation_binding()
    runtime.session.pipeline.rate_limiter = Quota()
    agent_input = QueryEnvelope(message=GOAL, owner=OWNER, session_id=SID, request_id='source',
                                channel='telegram', delivery_route=route).to_agent_input()
    events = [event async for event in runtime.session.run(agent_input)]
    assert events[-1].payload.get('status') == 'waiting'
    return (await runtime.store.load(owner=OWNER, session_id=SID)).metadata['read_wait']


@pytest.mark.parametrize('policy_changed', [False, True])
def test_cold_read_terminal_uses_same_outbox_and_ack_retry_without_reexecution(tg_config, policy_changed):
    async def run():
        clock = [100]
        candidate = await park(clock, ROUTE)
        assert candidate.get('delivery_route') == ROUTE
        if policy_changed:
            from app.modules.telegram_model_preferences import (
                set_telegram_model_preference,
            )
            set_telegram_model_preference(OWNER, SID, 'changed-model')
        clock[0] = 110
        runtime, script, reads = resumed(clock)
        script.adapter.settings = replace(script.adapter.settings, telegram_channel=provider_channel_fingerprint(config.get_many(("AGENT_LLM_API_URL", "AGENT_LLM_PROTOCOL", "AGENT_LLM_API_KEY"))))
        assert await runtime.session.resume_wait(candidate=candidate)
        assert not await runtime.session.resume_wait(candidate=candidate)
        state = await runtime.store.load(owner=OWNER, session_id=SID)
        record = state.metadata['read_wait']
        assert record['state'] == 'terminal' and 'delivery_pending' not in record
        saved_delivery, = state.metadata['continuation_deliveries'].values()
        assert saved_delivery['delivery_view']['status'] == ('failed' if policy_changed else 'success')
        assert state.metadata['active_publication']['status'] == 'finished'
        assert len(reads) == len(script.requests) == (0 if policy_changed else 1)

        from app.agent.kernel.continuation import deliver_continuation_result
        from app.bot.agent_adapter import (
            prepare_agent_continuation_delivery,
            publish_agent_continuation_delivery,
        )
        from app.modules import telegram_notification_center as notifications
        cold = SQLiteKernelStore(clock=lambda: clock[0])
        delivery, = await cold.due_continuations()
        assert delivery['kind'] == 'delivery'
        assert delivery['delivery_id'] == f'{candidate["wait_id"]}:{record["resume_turn_id"]}'
        with patch.object(notifications, 'wake_telegram_notification_dispatcher', return_value=False), patch.object(
            notifications, '_agent_interaction_enabled', return_value=True,
        ):
            with patch.object(cold, 'update_effect_state', side_effect=RuntimeError('outbox committed before ACK')):
                with pytest.raises(RuntimeError, match='before ACK'):
                    await deliver_continuation_result(
                        cold, delivery,
                        prepare_delivery=prepare_agent_continuation_delivery,
                        publish_delivery=publish_agent_continuation_delivery,
                    )
            with db.get_conn() as conn:
                rows = conn.execute("SELECT event_json FROM telegram_notification_outbox WHERE json_extract(event_json,'$._agent_continuation_delivery') IS NOT NULL").fetchall()
            assert len(rows) == 1
            saved = json.loads(rows[0]['event_json'])['_agent_continuation_delivery']
            assert saved['delivery_route'] == ROUTE
            assert ('模型策略已改变' if policy_changed else '已取得真实查询结果') in saved['html']

            # 冷调度只重投持久视图；原 READ、模型与任务创建都不能再次执行。
            def forbidden(*_args):
                raise AssertionError('delivery constructed a model runtime')
            scheduler = AgentJobsScheduler(clock=lambda: datetime.fromtimestamp(clock[0]), continuation_runtime_factory=forbidden)
            assert await asyncio.to_thread(scheduler._run_due_continuation_once) == 1
            assert await asyncio.to_thread(scheduler._run_due_continuation_once) == 0
            assert await cold.due_continuations() == []
            with db.get_conn() as conn:
                assert conn.execute('SELECT COUNT(*) FROM telegram_notification_outbox').fetchone()[0] == 1
        assert len(reads) == len(script.requests) == (0 if policy_changed else 1)

        # 实际 Bot 发送仍验证实时权限，不能因为已入队而绕过撤权。
        from app.bot import agent_adapter
        current_get = config.get
        with patch.object(config, 'get', side_effect=lambda key, default='': '8' if key == 'TG_AGENT_ALLOWED_USER_IDS' else current_get(key, default)), patch(
            'app.notifier.get_bot', side_effect=AssertionError('revoked route reached Bot'),
        ):
            result = agent_adapter._send_agent_continuation_part(saved)
        assert not result.ok and result.status_code == 403
    asyncio.run(run())


def test_historical_read_wait_without_route_never_guesses_topic(tg_config):
    async def run():
        clock = [100]
        candidate = await park(clock, {})
        clock[0] = 110
        runtime, script, reads = resumed(clock)
        assert await runtime.session.resume_wait(candidate=candidate)
        state = await runtime.store.load(owner=OWNER, session_id=SID)
        assert state.metadata['read_wait']['state'] == 'terminal'
        assert not state.metadata.get('continuation_deliveries')
        assert await runtime.store.due_continuations() == []
        assert len(reads) == len(script.requests) == 1
        with db.get_conn() as conn:
            assert conn.execute('SELECT COUNT(*) FROM telegram_notification_outbox').fetchone()[0] == 0
    asyncio.run(run())


def test_read_checkpoint_complete_keeps_claim_until_public_terminal(tg_config):
    class CheckpointScript(WaitScript):
        async def respond(self, request):
            if self.requests:
                return await super().respond(request)
            body = json.loads(request.content)
            self.requests.append(body)
            tasks = [json.loads(row['content'])['task'] for row in body['messages']
                     if row.get('role') == 'tool' and 'task' in json.loads(row['content'])]
            fact = tasks[-1]['verified_facts'][0]
            return httpx.Response(200, json={'choices': [{'message': {'content': '', 'tool_calls': [{
                'id': 'checkpoint-complete', 'type': 'function', 'function': task_function({'operation': 'checkpoint', 'evidence_ids': [], 'blockers': [],
                                         'read_evidence': {'read': fact['evidence_id']}}),
            }]}, 'finish_reason': 'tool_calls'}]})

    async def run():
        from tests.test_agent_task_first_slice import declaration
        task = declaration()
        task['obligations'] = [{'id': 'read', 'description': '取得本次查询回执', 'required': True,
                                'acceptance': {'kind': 'observed_read', 'criterion': '本次查询有真实回执'}}]
        clock = [100]
        with patch('tests.test_agent_read_wait_lifecycle.declaration', return_value=task):
            candidate = await park(clock, ROUTE)
        clock[0] = 110
        runtime, script, reads = resumed(clock, CheckpointScript(True))
        script.adapter.settings = replace(script.adapter.settings, telegram_channel=provider_channel_fingerprint(config.get_many(("AGENT_LLM_API_URL", "AGENT_LLM_PROTOCOL", "AGENT_LLM_API_KEY"))))
        assert await runtime.session.resume_wait(candidate=candidate)
        state = await runtime.store.load(owner=OWNER, session_id=SID)
        assert state.metadata['task']['status'] == 'complete'
        delivery, = await runtime.store.due_continuations()
        assert delivery['delivery_view']['status'] == 'success'
        assert delivery['delivery_view']['task']['status'] == 'complete'
        assert delivery['delivery_view']['answer'] == '已取得真实查询结果。'
        assert len(reads) == 1 and len(script.requests) == 2
    asyncio.run(run())
