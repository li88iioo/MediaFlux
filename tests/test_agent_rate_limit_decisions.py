"""Atomic limiter/port contracts, using real isolated SQLite and offline handlers.

Success quota display, Session projection and durable automatic waiting are not
implemented by this slice. These tests do not substitute for those contracts.
"""
from typing import cast
import operator
import asyncio
import hashlib
import json
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from types import MappingProxyType
from unittest.mock import patch

import pytest

from app import database as db
from app.agent import rate_limit as rates
from app.agent.errors import ToolPipelineError, ToolRateLimitError
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.conversation_context import tool_error_message
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.ports import mediaflux_policy as policy
from app.agent.kernel.ports.mediaflux_policy import MediaFluxToolRateLimiter
from app.agent.kernel.state import InMemorySessionStateStore
from app.agent.kernel.state_types import CancellationToken
from app.agent.kernel.tool_calls import ToolCallContext
from app.agent.rate_limit import AgentRateLimiter
from tests.support import isolated_test_database
from tests.test_agent_kernel_core import read_tool


@pytest.fixture(autouse=True)
def isolated_rate_database(monkeypatch):
    with isolated_test_database("atomic-rate-decisions.db") as path:
        # The lock contender must open the actual configured database.
        monkeypatch.setenv("MEDIAFLUX_TEST_DB_PATH", str(path))
        yield


def rows():
    with db.get_conn() as conn:
        return [tuple(row) for row in conn.execute(
            "SELECT * FROM agent_rate_limit_buckets ORDER BY limiter_key"
        )]


snapshot = rows


def test_rate_limit_error_has_one_contract_owner_without_port_alias():
    assert ToolRateLimitError.__module__ == "app.agent.errors"
    assert issubclass(ToolRateLimitError, ToolPipelineError)
    assert not hasattr(policy, "ToolRateLimitError")


def test_real_pipeline_shared_owner_and_retry_payload(monkeypatch):
    tool = read_tool("fixture.read")
    monkeypatch.setitem(rates._TOOL_RATE_LIMITS, tool.name, 8)
    calls=[]
    def read(args,ctx):
        calls.append((ctx.owner,ctx.session_id));return {'ok':True,'summary':'offline fixture'}
    async def progress(p):pass
    async def go():
        store=InMemorySessionStateStore()
        pipeline=ToolPipeline(catalog=ToolCatalog([replace(tool,read=read)]),state_store=store,rate_limiter=MediaFluxToolRateLimiter())
        async def query(owner='owner-a',session='s1',operation='list',cancel=False,arguments=None):
            lease,_=await store.begin_turn(owner=owner,session_id=session,request_id=str(len(calls)))
            token=CancellationToken()
            if cancel:token.cancel()
            ctx=ToolCallContext(owner=owner,session_id=session,request_id=lease.request_id,turn_id=lease.turn_id,lease=lease,cancellation=token,report_progress=progress)
            return await pipeline.execute(tool.model_name,{},context=ctx)
        for i in range(8):await query(session='s1' if i%2 else 's2')
        with pytest.raises(ToolPipelineError) as exc:await query(session='s3')
        assert exc.value.code=='rate_limited' and len(calls)==8
        await query(owner='owner-b');await query(owner='owner-c',operation='list')
        assert len(calls)==10
        with pytest.raises(asyncio.CancelledError):await query(owner='cancelled-owner',cancel=True)
        assert len(calls)==10
        from app.agent.kernel.model import ModelToolCall
        native=tool_error_message(ModelToolCall(call_id='native-28',name=tool.name,arguments={}),exc.value)
        data=json.loads(native.content)
        print('PIPELINE_ERROR',vars(exc.value),'NATIVE_ERROR',data,'HANDLERS',calls)
        # Positive contract: BASE must fail on precise metadata, not fixture.
        advice=getattr(exc.value,'rate_limit',{})
        assert advice.get('retry_after_seconds')==68
        assert advice.get('remaining')==0
        assert dict(advice)=={'limit':8,'cost':1,'remaining':0,'retry_after_seconds':68.0,'retry_at_epoch':188.0,'reason':'quota'}
        with pytest.raises(TypeError):advice['remaining']=99
        assert isinstance(exc.value,ToolRateLimitError)
    limiter=AgentRateLimiter(shared=True)
    limiter.reset()
    with patch('app.agent.rate_limit.time.time',return_value=120.0):
        asyncio.run(go())


def test_shared_weighted_boundary_and_monotonic_control():
    wall=[120.0];mono=[0.0]
    local=AgentRateLimiter(clock=lambda:mono[0])
    shared=AgentRateLimiter(shared=True,clock=lambda:mono[0]);shared.reset()
    with patch('app.agent.rate_limit.time.time',side_effect=lambda:wall[0]):
        assert local.allow('local',limit=1,window_seconds=60)
        for _ in range(8):assert shared.allow('shared',limit=8,window_seconds=60)
        mono[0]=60.0
        assert local.allow('local',limit=1,window_seconds=60)
        assert not shared.allow('shared',limit=8,window_seconds=60)
        wall[0]=180.0
        assert not shared.allow('shared',limit=8,window_seconds=60)
        wall[0]=187.0
        assert not shared.allow('shared',limit=8,window_seconds=60)
        wall[0]=188.0
        assert shared.allow('shared',limit=8,window_seconds=60)
    print('CLOCK shared-wall/in-memory-monotonic; earliest admit 188 after denial at120 =>68s')


def test_fractional_shared_cost_and_reopen():
    wall=[120.25]
    limiter=AgentRateLimiter(shared=True,wall_clock=lambda:wall[0]);limiter.reset()
    for _ in range(4):
        d=limiter.consume('fractional',limit=4,window_seconds=60)
        assert d.allowed and d.retry_after_seconds==0 and d.retry_at_epoch is None
    d=limiter.consume('fractional',limit=4,window_seconds=60,cost=2)
    assert (d.remaining,d.retry_after_seconds,d.retry_at_epoch,d.reason)==(0,89.75,210.0,'quota')
    with pytest.raises(FrozenInstanceError):setattr(d, 'remaining', 8)
    wall[0]=209.999
    reopened=AgentRateLimiter(shared=True,wall_clock=lambda:wall[0])
    before=rows()
    d=reopened.consume('fractional',limit=4,window_seconds=60,cost=2)
    assert not d.allowed and d.remaining==1 and d.retry_after_seconds==pytest.approx(.001)
    assert rows()==before
    wall[0]=210
    d=reopened.consume('fractional',limit=4,window_seconds=60,cost=2)
    assert d.allowed and d.remaining==0


def test_memory_cost_gap_and_bool_single_consumption():
    clock=[0.0]
    limiter=AgentRateLimiter(clock=lambda:clock[0])
    for t in (0,10,20,30):
        clock[0]=t
        assert limiter.allow('a',limit=4,window_seconds=60) is True
    d=limiter.consume('a',limit=4,window_seconds=60,cost=2)
    assert (d.remaining,d.retry_after_seconds,d.retry_at_epoch)==(0,40,None)
    clock[0]=60
    d=limiter.consume('a',limit=4,window_seconds=60,cost=2)
    assert not d.allowed and d.remaining==1 and d.retry_after_seconds==10
    clock[0]=70
    assert limiter.consume('a',limit=4,window_seconds=60,cost=2).allowed
    assert limiter.allow('a',limit=4,window_seconds=60) is False


@pytest.mark.parametrize('shared',[False,True])
def test_capacity_is_not_spent_quota(shared):
    clock=[120.25]
    limiter=AgentRateLimiter(max_keys=1,shared=shared,clock=lambda:clock[0],wall_clock=lambda:clock[0]);limiter.reset()
    assert limiter.consume('a',limit=1,window_seconds=60).allowed
    d=limiter.consume('b',limit=7,window_seconds=60,cost=2)
    assert not d.allowed and d.reason=='capacity' and d.remaining==7
    assert d.retry_after_seconds==(119.75 if shared else 60)
    assert d.retry_at_epoch==(240 if shared else None)
    assert d.retry_after_seconds is not None
    clock[0]+=d.retry_after_seconds
    assert limiter.consume('b',limit=7,window_seconds=60,cost=2).allowed


def test_rollback_keeps_persistent_row_and_recovers_without_refund():
    clock=[180.0]
    limiter=AgentRateLimiter(shared=True,wall_clock=lambda:clock[0]);limiter.reset()
    assert limiter.consume('a',limit=1,window_seconds=60).allowed
    before=rows();clock[0]=120
    d=limiter.consume('a',limit=1,window_seconds=60)
    assert not d.allowed and d.reason=='clock_anomaly'
    assert d.retry_after_seconds is None and d.retry_at_epoch is None
    assert rows()==before
    clock[0]=180
    assert limiter.allow('a',limit=1,window_seconds=60) is False
    clock[0]=300
    assert limiter.allow('a',limit=1,window_seconds=60) is True


def test_atomic_decisions_concurrency():
    AgentRateLimiter(shared=True).reset()
    barrier=threading.Barrier(12)
    def call(_):
        barrier.wait(timeout=5)
        return AgentRateLimiter(shared=True,wall_clock=lambda:120.25).consume('parallel',limit=8,window_seconds=60)
    with ThreadPoolExecutor(max_workers=12) as pool:decisions=list(pool.map(call,range(12)))
    assert sorted(d.remaining for d in decisions if d.allowed)==list(range(8))
    denied=[d for d in decisions if not d.allowed]
    assert len(denied)==4
    assert all((d.remaining,d.retry_after_seconds,d.retry_at_epoch)==(0,67.75,188) for d in denied)


def test_authoritative_cost_and_direct_consumption_share_bucket():
    limiter=AgentRateLimiter(shared=True,wall_clock=lambda:120.0);limiter.reset()
    async def go():
        port=policy.MediaFluxToolRateLimiter()
        await port.acquire(owner='cost-owner',tool_name='subscriptions.check_updates',cost=999,arguments={})
        assert rates.consume_agent_tool('cost-owner','subscriptions.check_updates').allowed is True
        with pytest.raises(ToolRateLimitError) as raised:
            await port.acquire(owner='cost-owner',tool_name='subscriptions.check_updates',cost=0,arguments={})
        assert dict(raised.value.rate_limit)=={'limit':4,'cost':2,'remaining':0,'retry_after_seconds':90.0,'retry_at_epoch':210.0,'reason':'quota'}
        with pytest.raises(AttributeError):setattr(raised.value, 'rate_limit', {})
        with db.get_conn() as conn:
            row=conn.execute('SELECT limiter_key,count FROM agent_rate_limit_buckets').fetchone()
        expected=hashlib.sha256(b'mediaflux-agent-rate:v1\0cost-owner:media-subscription-updates').hexdigest()
        assert tuple(row)==(expected,4)
    with patch.object(rates,'agent_rate_limiter',limiter):asyncio.run(go())




def test_cancel_during_atomic_worker_does_not_refund_or_replay():
    limiter=AgentRateLimiter(shared=True,wall_clock=lambda:120.0);limiter.reset()
    entered=threading.Event();release=threading.Event();finished=threading.Event()
    real=policy.consume_agent_tool
    def held(*args,**kwargs):
        entered.set()
        assert release.wait(5)
        try:return real(*args,**kwargs)
        finally:finished.set()
    async def go():
        task=asyncio.create_task(policy.MediaFluxToolRateLimiter().acquire(owner='cancel-owner',tool_name='fixture.lookup',cost=1,arguments={}))
        assert await asyncio.to_thread(entered.wait,5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        release.set()
        assert await asyncio.to_thread(finished.wait,5)
        with db.get_conn() as conn:
            assert conn.execute('SELECT count FROM agent_rate_limit_buckets').fetchone()['count']==1
    with patch.object(rates,'agent_rate_limiter',limiter),patch.object(policy,'consume_agent_tool',held):
        try:asyncio.run(go())
        finally:release.set()


@pytest.mark.parametrize('denial', ['quota', 'capacity', 'clock_anomaly'])
def test_real_port_public_values_are_immutable_json_scalars(denial):
    tool_name, limit, retry_wait = "fixture.lookup", 30, 62.0
    wall=[120.0]
    limiter=rates.AgentRateLimiter(shared=True,wall_clock=lambda:wall[0],max_keys=1)
    limiter.reset()
    owner='private-owner:/root/not-public'
    async def exercise():
        port=policy.MediaFluxToolRateLimiter()
        for _ in range(limit):
            assert await port.acquire(owner=owner,tool_name=tool_name,cost=999,arguments={'private':'never-project'}) is None
        if denial=='clock_anomaly':wall[0]=60.0
        denied_owner='different-owner' if denial=='capacity' else owner
        before=snapshot()
        with pytest.raises(ToolRateLimitError) as caught:
            await port.acquire(owner=denied_owner,tool_name=tool_name,cost=0,arguments={})
        error=caught.value
        assert error.code=='rate_limited' and error.task_patch is None
        projection=error.rate_limit
        assert type(projection) is MappingProxyType
        assert set(projection)=={'limit','cost','remaining','retry_after_seconds','retry_at_epoch','reason'}
        for key in ('limit','cost','remaining'):assert type(projection[key]) is int
        assert type(projection['reason']) is str
        for key in ('retry_after_seconds','retry_at_epoch'):
            assert projection[key] is None or type(projection[key]) is float
        assert projection['reason']==denial
        if denial=='quota':
            assert dict(projection)=={'limit':limit,'cost':1,'remaining':0,'retry_after_seconds':retry_wait,'retry_at_epoch':120.0+retry_wait,'reason':'quota'}
        elif denial=='capacity':
            assert dict(projection)=={'limit':limit,'cost':1,'remaining':limit,'retry_after_seconds':120.0,'retry_at_epoch':240.0,'reason':'capacity'}
        else:
            assert projection['retry_after_seconds'] is None and projection['retry_at_epoch'] is None
        serialized=json.dumps(dict(projection),allow_nan=False,sort_keys=True)
        assert 'private' not in serialized and '/root' not in serialized
        with pytest.raises(TypeError):operator.setitem(cast(dict[str, object], projection), 'remaining', 100)
        with pytest.raises(AttributeError):setattr(error, 'rate_limit', {})
        assert snapshot()==before
        limiter.reset()
        assert json.dumps(dict(projection),allow_nan=False,sort_keys=True)==serialized
        print('PUBLIC',denial,serialized,{k:type(v).__name__ for k,v in projection.items()})
    with patch.object(rates,'agent_rate_limiter',limiter):asyncio.run(exercise())


def test_each_decision_consumes_exactly_once():
    limiter=rates.AgentRateLimiter(shared=True,wall_clock=lambda:120.0)
    limiter.reset()
    with patch.object(rates,'agent_rate_limiter',limiter):
        first=rates.consume_agent_tool('owner','subscriptions.check_updates')
        assert first.allowed is True and first.remaining==2 and first.cost==2
        with pytest.raises(FrozenInstanceError):setattr(first, 'remaining', 999)
        assert rates.consume_agent_tool('owner','subscriptions.check_updates').allowed is True
        denied=rates.consume_agent_tool('owner','subscriptions.check_updates')
        assert denied.allowed is False and denied.remaining==0
        assert denied.retry_after_seconds==90.0 and denied.retry_at_epoch==210.0
        assert rates.consume_agent_tool('owner','subscriptions.check_updates').allowed is False
        with db.get_conn() as conn:
            rows=conn.execute('SELECT count,previous_count FROM agent_rate_limit_buckets').fetchall()
        assert [tuple(row) for row in rows]==[(4,0)]


def test_wall_snapshot_happens_once_under_real_write_lock():
    AgentRateLimiter(shared=True).reset()
    sampled=[]
    def clock():
        contender=sqlite3.connect(os.environ['MEDIAFLUX_TEST_DB_PATH'],timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError,match='locked'):
                contender.execute('BEGIN IMMEDIATE')
        finally:
            contender.close()
        sampled.append(120.25)
        return sampled[-1]
    limiter=AgentRateLimiter(shared=True,wall_clock=clock,max_keys=1)
    first=limiter.consume('a',limit=1,window_seconds=60)
    quota=limiter.consume('a',limit=1,window_seconds=60)
    capacity=limiter.consume('b',limit=1,window_seconds=60)
    assert len(sampled)==3
    assert first.allowed and first.remaining==0
    assert not quota.allowed and (quota.retry_after_seconds,quota.retry_at_epoch)==(119.75,240.0)
    assert not capacity.allowed and capacity.reason=='capacity'
    assert len(rows())==1
    print('LOCK: allowed/quota/capacity each sampled once under SQLite write lock; three samples')


def test_failed_permit_rolls_back_cleanup_and_releases_writer_lock():
    wall=[120.0]
    limiter=AgentRateLimiter(shared=True,wall_clock=lambda:wall[0]);limiter.reset()
    assert limiter.consume('expired',limit=2,window_seconds=60).allowed
    before=rows();wall[0]=240.0
    failure=RuntimeError('synthetic timestamp failure after cleanup')
    with patch.object(db,'now',side_effect=failure), pytest.raises(RuntimeError) as raised:
        limiter.consume('new',limit=2,window_seconds=60)
    assert raised.value is failure
    assert rows()==before
    # A fresh real transaction proves no leaked writer lock or hidden permit.
    reopened=AgentRateLimiter(shared=True,wall_clock=lambda:240.0)
    decision=reopened.consume('new',limit=2,window_seconds=60)
    assert decision.allowed and decision.remaining==1
    after=rows()
    assert len(after)==1 and after[0][0]!=before[0][0]
    with db.get_conn() as conn:
        assert tuple(conn.execute('SELECT count,previous_count,window_start FROM agent_rate_limit_buckets').fetchone())==(1,0,240)
    print('ROLLBACK: expired cleanup rolled back on failure; reopened transaction admitted exactly once')
