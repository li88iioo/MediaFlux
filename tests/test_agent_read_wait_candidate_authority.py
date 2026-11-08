"""Positive exact-candidate contracts across a genuinely native same-ID repark."""
import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from app import database as db
from app.agent.errors import ToolRateLimitError
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.rate_limit import RateLimitDecision
from tests.test_agent_read_wait_contract_repair import resumed
from tests.test_agent_read_wait_lifecycle import (
    GOAL,
    OWNER,
    parked_runtime,
)
from tests.test_agent_read_wait_lifecycle import (
    isolated_database as isolated_database,
)


@pytest.mark.parametrize('mode', ['stale_resume', 'stale_effect_cleanup', 'stale_binding_control', 'fresh_control'])
def test_same_wait_id_repark_preserves_current_authority(mode, tmp_path):
    async def run():
        clock = [100]
        _, stale = await parked_runtime(clock)
        clock[0] = 110
        scanned = await SQLiteKernelStore(clock=lambda: clock[0]).due_continuations()
        assert len(scanned) == 1 and scanned[0] == stale
        stale = scanned[0]  # Actual signed due-scan snapshot delayed by another worker.
        active, script, reads = resumed(clock)
        script.prefix = 'second-segment'

        class QuotaAgain:
            async def acquire(self, **kwargs):
                raise ToolRateLimitError(RateLimitDecision(False, 6, 1, 0,
                    retry_after_seconds=10, retry_at_epoch=120, reason='quota'))

        active.session.pipeline.rate_limiter = QuotaAgain()
        assert await active.session.resume_wait(candidate=stale)
        current = await active.store.load(owner=OWNER, session_id='wait-control')
        new = current.metadata['read_wait']
        assert new['wait_id'] == stale['wait_id']
        assert new['publication_generation'] > stale['publication_generation']
        assert new['claim_id'] != stale['claim_id']
        assert new['anchor']['call_id'] != stale['anchor']['call_id']
        assert new['budget']['wait_segments'] == 2 and new['state'] == 'pending'
        assert len(script.requests) == 0 and reads == []
        with db.get_conn() as conn:
            actual = conn.execute('PRAGMA database_list').fetchall()[0][2]
        assert Path(actual).resolve() == db.resolve_db_path().resolve()
        assert Path(actual).is_relative_to(tmp_path)
        preparations = []
        if mode == 'stale_effect_cleanup':
            # Same-publication trusted composition, as in accepted B08 fixture:
            # actual pipeline prepare, not a forged record or continued model.
            from app.agent.kernel.capabilities import KernelToolSpec, ToolEffect
            from app.agent.kernel.tool_calls import PreparedEffect
            from app.agent.kernel.state_types import CancellationToken, PublicationLease
            from app.agent.kernel.tool_calls import ToolCallContext
            def prepare(args, context):
                preparations.append(dict(args))
                return PreparedEffect(preview={'summary':'offline'}, snapshot_fingerprint='fixed')
            def execute(*args):
                raise AssertionError('must never execute WRITE')
            tool = KernelToolSpec(name='fixture.change', domain='library', description='offline',
                input_schema={'type':'object','properties':{},'additionalProperties':False},
                effect=ToolEffect.WRITE, prepare=prepare, execute_confirmed=execute)
            active.session.catalog._tools[tool.name] = tool
            # Restore normal limiter: the preceding quota only modeled READ denial.
            from tests.test_agent_read_wait_lifecycle import Quota
            active.session.pipeline.rate_limiter = Quota()
            lease = PublicationLease(OWNER, 'wait-control', current.generation, new['resume_turn_id'], 'source')
            async def progress(event):
                pass
            ctx = ToolCallContext(owner=OWNER, session_id='wait-control', lease=lease,
                request_id='source', turn_id=lease.turn_id, report_progress=progress,
                cancellation=CancellationToken(), user_source=GOAL)
            result = await active.session.pipeline.execute(tool.name, {}, context=ctx)
            assert result.effect_plan and preparations == [{}]
        clock[0] = 120
        fresh, last_script, last_reads = resumed(clock)
        last_script.prefix = 'third-segment'
        before = await fresh.store.load(owner=OWNER, session_id='wait-control')
        factories = []
        if mode == 'stale_effect_cleanup':
            from datetime import datetime
            from unittest.mock import patch

            from app.agent.feature_gate import (
                begin_runtime_change,
                finish_runtime_change,
                read_runtime_fence,
            )
            from app.modules.agent_jobs_scheduler import AgentJobsScheduler
            with db.get_conn() as conn:
                conn.execute('BEGIN IMMEDIATE')
                fence = read_runtime_fence(conn)
                finish_runtime_change(conn, begin_runtime_change(conn, fence), fence.config_digest)
            async def delayed_scan(store, *, limit=16):
                return [stale]
            def forbidden(*args):
                factories.append(True)
                raise AssertionError('retirement must not build a model runtime')
            scheduler = AgentJobsScheduler(clock=lambda: datetime.fromtimestamp(clock[0]), continuation_runtime_factory=forbidden)
            with patch.object(SQLiteKernelStore, 'due_continuations', delayed_scan):
                outcome = await asyncio.to_thread(scheduler._run_due_continuation_once)
            assert factories == []
        elif mode == 'stale_binding_control':
            assert fresh.session.continuation_binding is not None
            binding = replace(fresh.session.continuation_binding, effective_digest='stale-snapshot')
            outcome = await fresh.store.claim_continuation(stale, binding)
        else:
            outcome = await fresh.session.resume_wait(candidate=new if mode == 'fresh_control' else stale)
        after = await SQLiteKernelStore(clock=lambda: clock[0]).load(owner=OWNER, session_id='wait-control')
        if mode == 'fresh_control':
            assert outcome and len(last_script.requests) == 1 and len(last_reads) == 1
            assert after.metadata['read_wait']['terminal_reason'] == 'completed'
            assert last_script.requests[0]['messages'][0] is not None
        else:
            assert after.to_payload() == before.to_payload(), 'stale scanned candidate mutated newer same-ID repark authority'
            assert not outcome and last_script.requests == [] and last_reads == []
    asyncio.run(run())
