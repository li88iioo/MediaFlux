"""元数据SDK与原EffectExecutor共用公共预检/事务，全部使用隔离SQLite。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app import config, database as db, sdk
from app.repositories import strm as strm_repo
from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.errors import ToolPipelineError
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.effects import ConfirmationEffectPlanStore, EffectExecutor
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.ports.mediaflux_effects import MediaFluxEffectLifecycle
from app.agent.kernel.ports.mediaflux_policy import MediaFluxAuthorizationPolicy
from app.agent.kernel.ports.sdk_effects import strm_metadata_tools
from app.agent.kernel.state_types import CancellationToken
from app.agent.kernel.tool_calls import ToolCallContext
from app.modules import strm_metadata_management as service
from tests.support import initialized_web_runtime
from tests.test_strm_metadata_queue import _job

OWNER = "webk:v1:" + "a" * 64


@pytest.fixture
def metadata_backend(tmp_path, monkeypatch):
    with initialized_web_runtime() as paths:
        config.set_and_save({"STRM_METADATA_ENABLED": "false"})
        worker = Mock()
        worker.status.side_effect = lambda: {**strm_repo.count_strm_metadata_jobs(),
            "enabled": config.get_bool("STRM_METADATA_ENABLED", False),
            "worker_running": True, "consumer_active": True}
        monkeypatch.setattr("app.modules.strm_metadata_worker.get_strm_metadata_worker", lambda: worker)
        yield SimpleNamespace(paths=paths, root=tmp_path, worker=worker)


async def _context(store, *, owner=OWNER, session="metadata-session"):
    lease, _ = await store.begin_turn(owner=owner, session_id=session, request_id="metadata-test")
    return ToolCallContext(owner=owner, session_id=session, request_id=lease.request_id,
        turn_id=lease.turn_id, lease=lease, cancellation=CancellationToken(), report_progress=AsyncMock(), channel="web")


def _executor(store):
    return EffectExecutor(catalog=ToolCatalog(strm_metadata_tools()), state_store=store, reference_store=store,
        effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore()),
        effect_lifecycle=MediaFluxEffectLifecycle(), authorization=MediaFluxAuthorizationPolicy())


def _states():
    with db.get_conn() as conn:
        return {row["file_id"]: row["status"] for row in conn.execute("SELECT file_id,status FROM strm_metadata_queue")}


def test_queue_cancel_prepares_and_rechecks_after_explicit_confirmation(metadata_backend):
    strm_repo.enqueue_strm_metadata_jobs([_job(file_id="old")])
    existing = metadata_backend.root / "movie.nfo"
    existing.write_text("existing metadata")
    async def check():
        store = SQLiteKernelStore()
        context = await _context(store)
        effects = _executor(store)
        plan = (await effects.prepare("strm.cancel_metadata_pending", {}, context=context)).effect_plan
        assert plan is not None and plan.preview["data"]["count"] == 1
        assert _states() == {"old": "queued"}
        other = await _context(store, owner="webk:v1:" + "b" * 64)
        with pytest.raises(ToolPipelineError):
            await effects.execute_confirmed(plan.plan_id, context=other)
        strm_repo.enqueue_strm_metadata_jobs([_job(file_id="new")])
        effects = _executor(SQLiteKernelStore())
        restored = await effects.get_pending_plan(owner=context.owner, session_id=context.session_id)
        assert restored is not None and restored.plan_id == plan.plan_id
        result = (await effects.execute_confirmed(plan.plan_id, context=context)).outcome.public_content
        assert result["ok"] and result["status"] == "completed"
        assert result["data"]["cancelled"] == 1 and result["data"]["files_deleted"] == 0
        assert not result["data"]["enabled"]
        assert _states() == {"old": "cancelled", "new": "queued"}
        assert existing.read_text() == "existing metadata"
        with pytest.raises(ToolPipelineError):
            await effects.execute_confirmed(plan.plan_id, context=context)
    asyncio.run(check())
    metadata_backend.worker.wake.assert_not_called()


def test_changed_snapshot_fails_closed_in_confirmation_pipeline(metadata_backend):
    strm_repo.enqueue_strm_metadata_jobs([_job()])
    async def check():
        store = SQLiteKernelStore()
        context = await _context(store)
        effects = _executor(store)
        plan = (await effects.prepare("strm.cancel_metadata_pending", {}, context=context)).effect_plan
        assert plan is not None
        strm_repo.enqueue_strm_metadata_jobs([_job(etag="changed")])
        with pytest.raises(ToolPipelineError) as error:
            await effects.execute_confirmed(plan.plan_id, context=context)
        assert error.value.code == "confirmation_stale" and error.value.execution_started is False
        assert strm_repo.count_strm_metadata_jobs()["queued"] == 1
    asyncio.run(check())


def test_disable_does_not_cancel_queue_or_kill_running(metadata_backend):
    config.set_and_save({"STRM_METADATA_ENABLED": "true"})
    strm_repo.enqueue_strm_metadata_jobs([_job(file_id="queued"), _job(file_id="running")])
    with db.get_conn() as conn:
        conn.execute("UPDATE strm_metadata_queue SET status='running' WHERE file_id='running'")
    async def check():
        store = SQLiteKernelStore()
        context = await _context(store)
        effects = _executor(store)
        plan = (await effects.prepare("strm.set_metadata_enabled", {"enabled": False}, context=context)).effect_plan
        assert plan is not None
        assert plan.preview["data"]["before"] is True and plan.preview["data"]["enabled"] is False
        assert config.get_bool("STRM_METADATA_ENABLED") is True
        result = (await effects.execute_confirmed(plan.plan_id, context=context)).outcome.public_content
        assert result["ok"] and result["status"] == "completed"
        assert result["data"]["verified"] is True and result["data"]["enabled"] is False
    asyncio.run(check())
    assert config.get_bool("STRM_METADATA_ENABLED") is False
    assert _states() == {"queued": "queued", "running": "running"}
    metadata_backend.worker.wake.assert_called_once()
    metadata_backend.worker.stop.assert_not_called()


@pytest.mark.parametrize("change", ["owner", "expiry", "config", "cancel", "unknown", "unverified"])
def test_policy_rejections_and_unknown_results_keep_truthful_receipts(metadata_backend, monkeypatch, change):
    async def check():
        store = SQLiteKernelStore()
        context = await _context(store)
        effects = _executor(store)
        if change == "expiry":
            monkeypatch.setattr(service, "_PREVIEW_TTL", -1)
        plan = (await effects.prepare("strm.set_metadata_enabled", {"enabled": True}, context=context)).effect_plan
        assert plan is not None
        if change == "owner":
            context = await _context(store, owner="webk:v1:" + "b" * 64)
        elif change == "config":
            config.set_and_save({"STRM_METADATA_ENABLED": "true"})
        elif change == "cancel":
            assert await effects.cancel_effect(plan.plan_id, lease=context.lease)
        elif change == "unknown":
            monkeypatch.setattr(service, "set_policy_confirmed", Mock(side_effect=RuntimeError("provider boundary unknown")))
        elif change == "unverified":
            monkeypatch.setattr(service, "set_policy_confirmed", lambda *_args: {
                "enabled": False, "verified": False, "summary": "写后运行态未核验一致"})
        if change == "unverified":
            result = (await effects.execute_confirmed(plan.plan_id, context=context)).outcome.public_content
            assert result["status"] == "outcome_unknown" and result["ok"] is False
            assert result["data"]["verified"] is False
        else:
            with pytest.raises(ToolPipelineError) as error:
                await effects.execute_confirmed(plan.plan_id, context=context)
            if change == "unknown":
                assert error.value.execution_started is True
                assert "provider boundary" not in str(error.value)
            elif change in {"expiry", "config"}:
                assert error.value.code == "confirmation_stale" and error.value.execution_started is False
        assert strm_repo.count_strm_metadata_jobs()["total"] == 0
    asyncio.run(check())


def test_empty_queue_prepare_has_no_false_used_confirmation_receipt(metadata_backend):
    async def check():
        store = SQLiteKernelStore()
        context = await _context(store)
        with pytest.raises(ToolPipelineError) as error:
            await _executor(store).prepare("strm.cancel_metadata_pending", {}, context=context)
        assert error.value.code == "precondition_failed" and error.value.execution_started is None
        assert "原确认已使用" not in str(error.value)
        assert not (await store.load(owner=context.owner, session_id=context.session_id)).pending_effect_plan_id
    asyncio.run(check())


def test_plain_sdk_control_and_status_use_public_service(metadata_backend):
    strm_repo.enqueue_strm_metadata_jobs([_job()])
    with sdk.call_owner(OWNER):
        assert sdk.strm.metadata_status()["queued"] == 1
        policy = sdk.strm.set_metadata_enabled(False)
        assert policy["verified"] is True and policy["enabled"] is False
        cancelled = sdk.strm.cancel_metadata_pending()
    assert cancelled["cancelled"] == 1 and cancelled["files_deleted"] == 0
    assert strm_repo.count_strm_metadata_jobs()["cancelled"] == 1


def test_concurrent_native_confirmations_cancel_only_one_frozen_set(metadata_backend):
    strm_repo.enqueue_strm_metadata_jobs([_job()])
    async def check():
        store = SQLiteKernelStore()
        contexts = [await _context(store, session=name) for name in ("first", "second")]
        effects = [_executor(store), _executor(store)]
        plans = []
        for executor, context in zip(effects, contexts):
            plan = (await executor.prepare("strm.cancel_metadata_pending", {}, context=context)).effect_plan
            assert plan is not None
            plans.append(plan)
        results = await asyncio.gather(*(executor.execute_confirmed(plan.plan_id, context=context)
            for executor, plan, context in zip(effects, plans, contexts)), return_exceptions=True)
        successful = [item.outcome.public_content for item in results if not isinstance(item, BaseException)]
        rejected = [item for item in results if isinstance(item, ToolPipelineError)]
        assert len(successful) == len(rejected) == 1
        assert successful[0]["data"]["cancelled"] == 1 and successful[0]["status"] == "completed"
        assert rejected[0].code == "confirmation_stale" and rejected[0].execution_started is False
    asyncio.run(check())
    assert strm_repo.count_strm_metadata_jobs()["cancelled"] == 1
