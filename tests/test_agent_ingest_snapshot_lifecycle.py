"""入库只消费真实会话引用；冷重建与生命周期变化不依赖进程缓存。"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from app import database as db
from app.agent.errors import ToolPipelineError
from tests.support import initialized_web_runtime
from tests.test_agent_effect_admission import _OfflineClient, _offline_runtime
from tests.test_agent_guangya_preview_lifecycle import OWNER, runtime_for_test
from tests.test_agent_kernel_existing_ports import context_for

SHARE = "https://www.guangyapan.com/s/1938060615538466851_aecd7hu726g3whGl#/share"


@pytest.mark.parametrize("cold", [False, True], ids=["warm", "cold"])
@pytest.mark.parametrize("operation", ["reset", "delete"])
def test_native_share_confirmation_is_session_bound_and_revoked_by_lifecycle(tmp_path, cold, operation):
    from app.modules import share_transfer
    from tests.test_telegram_guangya_share import FakeShareClient
    async def run():
        client = FakeShareClient()
        with initialized_web_runtime(), _offline_runtime(tmp_path, _OfflineClient()), patch.object(
            share_transfer, "GuangYaClient", return_value=client,
        ):
            runtime = runtime_for_test()
            context = await context_for(runtime.store, owner=OWNER, session="share-a")
            other = await context_for(runtime.store, owner=OWNER, session="share-b")
            args = {"url": SHARE, "file_ids": ["file-1"]}
            first = (await runtime.session.pipeline.prepare("cloud.transfer_share", args, context=context)).effect_plan
            second = (await runtime.session.pipeline.prepare("cloud.transfer_share", args, context=other)).effect_plan
            assert first is not None and second is not None
            if cold:
                runtime = runtime_for_test()
            with pytest.raises(ToolPipelineError):
                await runtime.session.pipeline.execute_confirmed(first.plan_id, context=other)
            await getattr(runtime.lifecycle, operation)(owner=OWNER, session_id="share-a")
            if cold:
                runtime = runtime_for_test()
            fresh = await context_for(runtime.store, owner=OWNER, session="share-a")
            with pytest.raises(ToolPipelineError):
                await runtime.session.pipeline.execute_confirmed(first.plan_id, context=fresh)
            unaffected = await runtime.session.pipeline.get_pending_plan(owner=OWNER, session_id="share-b")
            assert unaffected is not None and unaffected.plan_id == second.plan_id
            assert (await runtime.session.pipeline.prepare("cloud.transfer_share", args, context=fresh)).effect_plan
            assert not client.restore_calls
            with db.get_conn() as conn:
                assert conn.execute("SELECT COUNT(*) FROM download_requests").fetchone()[0] == 0
    asyncio.run(run())


@pytest.mark.parametrize('operation', ['reset', 'delete'])
def test_native_url_plan_is_session_bound_and_revoked_by_lifecycle(tmp_path, operation):
    async def run():
        with initialized_web_runtime(), _offline_runtime(tmp_path, _OfflineClient()), patch(
            'app.indexers.downloads.download_target_readiness', return_value={'qb': True},
        ), patch('app.indexers.downloads.dispatch_request') as dispatch:
            runtime = runtime_for_test()
            context = await context_for(runtime.store, owner=OWNER, session='url-a')
            other = await context_for(runtime.store, owner=OWNER, session='url-b')
            args = {'url': 'magnet:?xt=urn:btih:' + 'a' * 40, 'target': 'qb'}
            first = (await runtime.session.pipeline.prepare('downloads.submit', args, context=context)).effect_plan
            second = (await runtime.session.pipeline.prepare('downloads.submit', args, context=other)).effect_plan
            assert first is not None and second is not None
            with pytest.raises(ToolPipelineError):
                await runtime.session.pipeline.execute_confirmed(first.plan_id, context=other)
            await getattr(runtime.lifecycle, operation)(owner=OWNER, session_id='url-a')
            runtime = runtime_for_test()
            fresh = await context_for(runtime.store, owner=OWNER, session='url-a')
            with pytest.raises(ToolPipelineError):
                await runtime.session.pipeline.execute_confirmed(first.plan_id, context=fresh)
            unaffected = await runtime.session.pipeline.get_pending_plan(owner=OWNER, session_id='url-b')
            assert unaffected is not None and unaffected.plan_id == second.plan_id
            assert (await runtime.session.pipeline.prepare('downloads.submit', args, context=fresh)).effect_plan
            dispatch.assert_not_called()
            with db.get_conn() as conn:
                assert conn.execute('SELECT COUNT(*) FROM download_requests').fetchone()[0] == 0
    asyncio.run(run())
