"""真实完成回执的旧身份归一、租约 CAS、故障重试与结果未知隔离。"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager
from copy import deepcopy
from unittest.mock import patch

import pytest

from app import database as db
from app.agent import effect_completion as effects
from app.agent.kernel.completion_receipts import _completion_cipher
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.session_guard import session_scope_guard
from app.modules import telegram_notification_center as center
from app.notifier import TelegramSendResult
from app.repositories import telegram_notifications as notifications
from tests.support import agent_llm_enabled as agent_llm_enabled  # noqa: PLC0414
from tests.test_agent_continuation_delivery_handoff import (
    OWNER,
    ROUTE,
    SID,
    _completed_effect,
)


def _row(event_key):
    row = notifications.get_notification(event_key)
    assert row is not None
    return row


def _effect_row():
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM telegram_notification_outbox "
            "WHERE event_key LIKE 'tg:event:agent:agent-effect:%'",
        ).fetchall()
    assert len(rows) == 1
    return dict(rows[0])


def _due(event_key):
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE telegram_notification_outbox SET next_attempt_at='2000-01-01 00:00:00' WHERE event_key=?",
            (event_key,),
        )


@asynccontextmanager
async def _case(tmp_path, *, legacy=False):
    with patch.object(center, '_agent_interaction_enabled', return_value=True), patch.object(
        center, 'wake_telegram_notification_dispatcher', return_value=False,
    ):
        async with _completed_effect(tmp_path, [100]) as (store, script, reads):
            row = _effect_row()
            stored = json.loads(row['event_json'])
            delivery = deepcopy(stored['_agent_effect_delivery'])
            assert delivery['owner'] == OWNER and delivery['session_id'] == SID
            assert delivery['delivery_route'] == ROUTE
            if legacy:
                stored.pop('_agent_effect_delivery')
                with db.get_conn() as conn:
                    conn.execute('UPDATE telegram_notification_outbox SET event_json=? WHERE id=?',
                        (json.dumps(stored, ensure_ascii=False), row['id']))
                row = _row(row['event_key'])
            yield store, row, delivery, script, reads


def test_signed_legacy_receipt_is_bound_in_place_without_resetting_delivery_state(agent_llm_enabled, tmp_path):
    async def run():
        async with _case(tmp_path, legacy=True) as (_store, row, delivery, _script, _reads):
            with db.get_conn() as conn:
                conn.execute('UPDATE telegram_notification_outbox '
                    'SET attempts=3,message_id=731,revision=7,delivered_revision=2 WHERE id=?', (row['id'],))
            claimed, = notifications.claim_due_notifications(limit=1, event_key=row['event_key'])
            before = _row(row['event_key'])
            seen = []

            def edit(event, **kwargs):
                bound = _row(row['event_key'])
                assert json.loads(bound['event_json'])['_agent_effect_delivery'] == delivery
                assert {key: value for key, value in bound.items() if key != 'event_json'} == {
                    key: value for key, value in before.items() if key != 'event_json'}
                assert kwargs == {'chat_id': ROUTE['chat_id'], 'message_id': 731}
                seen.append(bound)
                return TelegramSendResult(ok=True, message_id=731)

            with patch.object(center, 'edit_event_result', side_effect=edit), patch.object(
                center, 'send_event_result', side_effect=AssertionError('已知消息应原位编辑'),
            ):
                assert center._dispatch_item(claimed)
            assert len(seen) == 1
            final = _effect_row()
            assert final['id'] == row['id'] and final['event_key'] == row['event_key']
            assert final['revision'] == final['delivered_revision'] == 7
            assert final['message_id'] == 731 and final['status'] == 'sent'
    asyncio.run(run())


@pytest.mark.parametrize('changed', ['lease', 'revision', 'event_json', 'status'])
def test_legacy_identity_cas_rejects_a_stale_claim_without_mutating_the_row(agent_llm_enabled, tmp_path, changed):
    async def run():
        async with _case(tmp_path, legacy=True) as (_store, row, delivery, _script, _reads):
            claimed, = notifications.claim_due_notifications(limit=1, event_key=row['event_key'])
            stored = json.loads(claimed['event_json'])
            stored['_agent_effect_delivery'] = delivery
            generation, revision, expected = claimed['lease_generation'], claimed['revision'], claimed['event_json']
            if changed == 'lease':
                generation += 1
            elif changed == 'revision':
                revision += 1
            elif changed == 'event_json':
                expected += ' '
            else:
                with db.get_conn() as conn:
                    conn.execute("UPDATE telegram_notification_outbox SET status='retry_wait' WHERE id=?", (row['id'],))
            before = _row(row['event_key'])
            assert notifications.bind_agent_effect_delivery(row['id'], lease_generation=generation,
                claimed_revision=revision, expected_event_json=expected,
                event_json=json.dumps(stored, ensure_ascii=False)) is None
            assert _row(row['event_key']) == before
    asyncio.run(run())


@pytest.mark.parametrize('damage', ['missing_seed', 'bad_cipher', 'seed_owner', 'seed_session', 'seed_plan', 'missing_route', 'signed_state', 'route'])
def test_legacy_receipt_without_a_matching_trusted_seed_is_suppressed(agent_llm_enabled, tmp_path, damage):
    async def run():
        async with _case(tmp_path, legacy=True) as (store, row, delivery, _script, _reads):
            if damage == 'signed_state':
                with db.get_conn() as conn:
                    conn.execute("UPDATE agent_kernel_sessions SET state_json=json_set(state_json,'$.summary','损坏签名')")
            elif damage == 'route':
                stored = json.loads(row['event_json'])
                stored['_delivery_route'] = {'chat_id': ROUTE['chat_id'], 'message_thread_id': 999}
                with db.get_conn() as conn:
                    conn.execute('UPDATE telegram_notification_outbox SET event_json=? WHERE id=?',
                        (json.dumps(stored, ensure_ascii=False), row['id']))
            else:
                def corrupt(state):
                    waits = state.metadata['effect_waits']
                    if damage == 'missing_seed':
                        waits.pop(delivery['delivery_id'])
                        return
                    wait = waits[delivery['delivery_id']]
                    if damage == 'bad_cipher':
                        wait['sealed'] = 'not-an-authenticated-seed'
                        return
                    seed = json.loads(_completion_cipher().decrypt(wait['sealed'].encode()))
                    if damage == 'missing_route':
                        seed['origin'].pop('delivery_route')
                    else:
                        field = {'seed_owner': 'owner', 'seed_session': 'session_id', 'seed_plan': 'plan_id'}[damage]
                        seed[field] = 'different-identity'
                    wait['sealed'] = _completion_cipher().encrypt(json.dumps(seed).encode()).decode()
                await store.update_effect_state(owner=OWNER, session_id=SID, change=corrupt)

            with patch.object(center, 'send_event_result') as sender, patch.object(center, 'edit_event_result') as editor:
                center.drain_telegram_notifications(limit=1, event_key=row['event_key'])
            sender.assert_not_called()
            editor.assert_not_called()
            final = _row(row['event_key'])
            assert final['status'] == 'suppressed'
            assert final['last_error'].startswith(('AgentInteraction', 'InvalidAgent'))
            assert final['id'] == row['id'] and final['event_key'] == row['event_key']
            assert '_agent_effect_delivery' not in json.loads(final['event_json'])
    asyncio.run(run())


def test_legacy_chat_only_seed_recovers_only_its_signed_owner_chat(agent_llm_enabled, tmp_path):
    async def run():
        async with _case(tmp_path, legacy=True) as (store, row, delivery, _script, _reads):
            def omit_old_origin_route(state):
                wait = state.metadata['effect_waits'][delivery['delivery_id']]
                seed = json.loads(_completion_cipher().decrypt(wait['sealed'].encode()))
                seed['origin'].pop('delivery_route')
                wait['sealed'] = _completion_cipher().encrypt(json.dumps(seed).encode()).decode()

            await store.update_effect_state(owner=OWNER, session_id=SID, change=omit_old_origin_route)
            chat_only = {'chat_id': ROUTE['chat_id']}
            stored = json.loads(row['event_json'])
            stored['_delivery_route'] = chat_only
            with db.get_conn() as conn:
                conn.execute('UPDATE telegram_notification_outbox SET event_json=? WHERE id=?',
                    (json.dumps(stored, ensure_ascii=False), row['id']))
            with patch.object(center, 'send_event_result', return_value=TelegramSendResult(ok=True, message_id=885)) as sender:
                assert center.drain_telegram_notifications(limit=1, event_key=row['event_key'])
            sender.assert_called_once()
            assert sender.call_args.kwargs == chat_only
            final = _row(row['event_key'])
            assert final['id'] == row['id'] and final['event_key'] == row['event_key']
            assert json.loads(final['event_json'])['_agent_effect_delivery'] == {**delivery, 'delivery_route': chat_only}
            assert final['status'] == 'sent'
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['database', 'session_lock'])
def test_temporary_identity_check_failure_retries_without_sending_or_quarantining(agent_llm_enabled, tmp_path, failure):
    async def run():
        async with _case(tmp_path) as (_store, row, _delivery, _script, _reads):
            with patch.object(center, 'send_event_result', return_value=TelegramSendResult(ok=True, message_id=883)) as sender:
                if failure == 'database':
                    with patch.object(SQLiteKernelStore, 'load_existing_session', side_effect=sqlite3.OperationalError('locked')):
                        assert not center.drain_telegram_notifications(limit=1, event_key=row['event_key'])
                else:
                    with session_scope_guard(OWNER, SID):
                        assert not center.drain_telegram_notifications(limit=1, event_key=row['event_key'])
                sender.assert_not_called()
                retry = _row(row['event_key'])
                assert retry['status'] == 'retry_wait' and retry['attempts'] == 1
                assert retry['event_json'] == row['event_json']
                _due(row['event_key'])
                assert center.drain_telegram_notifications(limit=1, event_key=row['event_key'])
            sender.assert_called_once()
            assert sender.call_args.kwargs == ROUTE
            assert _row(row['event_key'])['status'] == 'sent'
    asyncio.run(run())


def test_unknown_first_effect_send_is_not_replayed_by_publish_or_recovery(agent_llm_enabled, tmp_path):
    async def run():
        async with _case(tmp_path) as (_store, row, delivery, _script, _reads):
            event = center.deserialize_notification_event(row['event_json'])
            logical_key = center._effect_delivery_key(delivery)
            with patch.object(center, 'send_event_result', return_value=TelegramSendResult(
                ok=False, error='ReadTimeout', status_code=408,
            )) as sender:
                assert not center.drain_telegram_notifications(limit=1, event_key=row['event_key'])
                unknown = _row(row['event_key'])
                assert unknown['status'] == 'outcome_unknown' and not unknown['message_id']
                for _ in range(2):
                    again = center.publish_agent_interaction_reply(logical_key, event, delivery=delivery, deliver_now=True)
                    assert again.event_key == row['event_key'] and again.status == 'outcome_unknown'
                    notifications.recover_notifications()
                    assert not center.drain_telegram_notifications(limit=1, event_key=row['event_key'])
                sender.assert_called_once()
            assert _effect_row() == unknown
    asyncio.run(run())


def test_effect_outbox_ack_retry_preserves_one_row_and_sends_once(agent_llm_enabled, tmp_path):
    async def run():
        async with _case(tmp_path) as (store, row, delivery, script, reads):
            def due(state):
                state.metadata['effect_waits'][delivery['delivery_id']].update(delivered=False, next_poll_at=0)
                state.metadata['effect_next_poll_at'] = 0

            await store.update_effect_state(owner=OWNER, session_id=SID, change=due)
            update, enqueue = store.update_effect_state, effects._enqueue_late_receipt
            committed = [False]

            def handoff(*args, **kwargs):
                result = enqueue(*args, **kwargs)
                committed[0] = result
                return result

            async def interrupted_ack(**kwargs):
                if committed[0]:
                    committed[0] = False
                    raise sqlite3.OperationalError('fault injection: outbox committed before kernel ACK')
                return await update(**kwargs)

            with patch.object(store, 'update_effect_state', side_effect=interrupted_ack), patch.object(
                effects, '_enqueue_late_receipt', side_effect=handoff,
            ):
                assert await effects.poll_effect_receipts(store) == 0
            assert _effect_row() == row
            state = await store.load(owner=OWNER, session_id=SID)
            assert state.metadata['effect_waits'][delivery['delivery_id']]['delivered'] is False
            await store.update_effect_state(owner=OWNER, session_id=SID, change=due)
            assert await effects.poll_effect_receipts(store) == 1
            assert _effect_row() == row
            assert (await store.load(owner=OWNER, session_id=SID)).metadata['effect_waits'][delivery['delivery_id']]['delivered']
            with patch.object(center, 'send_event_result', return_value=TelegramSendResult(ok=True, message_id=884)) as sender:
                assert center.drain_telegram_notifications(limit=1, event_key=row['event_key'])
                assert center.drain_telegram_notifications(limit=1, event_key=row['event_key'])
            sender.assert_called_once()
            assert len(reads) == 1 and len(script.requests) == 2
    asyncio.run(run())
