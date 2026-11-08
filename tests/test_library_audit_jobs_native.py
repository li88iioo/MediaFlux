"""后台审计原生SDK/SQLite确认、真实job_id、裁剪事实与批次取消。"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import database as db
from app import sdk
from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.errors import ToolPipelineError, ToolRateLimitError
from app.agent.kernel import sandbox
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.effects import ConfirmationEffectPlanStore, EffectExecutor
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.ports.mediaflux_effects import MediaFluxEffectLifecycle
from app.agent.kernel.ports.sdk_effects import library_audit_job_tools
from app.modules import library_audit_jobs as service
from app.modules.agent_jobs_scheduler import AgentJobsScheduler
from app.modules.library_patrol import empty_patrol_projection
from app.repositories import agent_jobs as repository
from tests.support import isolated_test_database
from tests.test_agent_durable_jobs import _result
from tests.test_agent_sdk_effects import context_for

AS_OF = "2026-08-09"
OWNER = "audit-native-owner"
ARGS = {"as_of": AS_OF, "max_series": 2}


@pytest.fixture
def backend(monkeypatch):
    with isolated_test_database():
        worker = AgentJobsScheduler(
            audit_executor=Mock(
                return_value=_result(ok=True, status="up_to_date", checked=2)
            ),
            clock=lambda: datetime(2099, 1, 1, tzinfo=UTC),
        )
        worker.wake = Mock(wraps=worker.wake)
        monkeypatch.setattr(
            "app.modules.agent_jobs_scheduler.get_agent_jobs_scheduler", lambda: worker
        )
        yield worker
        worker.stop()


def effects(store):
    return EffectExecutor(
        catalog=ToolCatalog(library_audit_job_tools()),
        state_store=store,
        reference_store=store,
        effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore()),
        effect_lifecycle=MediaFluxEffectLifecycle(),
    )


def rows():
    with db.get_conn() as conn:
        return [
            dict(r) for r in conn.execute("SELECT * FROM agent_jobs ORDER BY job_id")
        ]


async def start_plan(store, context, **arguments):
    result = await effects(store).prepare(
        "library.start_audit", arguments or ARGS, context=context
    )
    assert result.effect_plan is not None
    return result.effect_plan


def test_public_service_import_and_sdk_discovery_do_not_load_agent():
    source = """import builtins
real=builtins.__import__
def guard(name,*args,**kwargs):
    if name=="app.agent" or name.startswith("app.agent."):raise AssertionError(name)
    return real(name,*args,**kwargs)
builtins.__import__=guard
from app.modules import library_audit_jobs
from app import sdk
assert {"library.start_audit","library.audit_job","library.cancel_audit"} <= sdk.METHODS.keys()
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", source],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "arguments",
    [
        {"as_of": "2099-01-01"},
        {"as_of": False},
        {"max_series": 0},
        {"max_series": True},
        {"max_series": 101},
    ],
)
def test_invalid_arguments_never_enqueue(backend, arguments):
    with pytest.raises((TypeError, ValueError)):
        service.prepare_start_audit(owner=OWNER, **arguments)
    assert not rows()
    backend.wake.assert_not_called()


def test_native_start_preserves_shared_patrol_rate_budget(backend, monkeypatch):
    from app.agent import rate_limit
    from app.agent.kernel.ports.mediaflux_policy import MediaFluxToolRateLimiter

    limiter = rate_limit.AgentRateLimiter(shared=True, wall_clock=lambda: 120.0)
    limiter.reset()
    monkeypatch.setattr(rate_limit, "agent_rate_limiter", limiter)

    async def run():
        port = MediaFluxToolRateLimiter()
        for name in ("library.start_audit", "library.run_patrol"):
            await port.acquire(owner=OWNER, tool_name=name, cost=1, arguments={})
        with pytest.raises(ToolRateLimitError) as denied:
            await port.acquire(
                owner=OWNER, tool_name="library.start_audit", cost=1, arguments={}
            )
        assert (
            denied.value.code == "rate_limited"
            and denied.value.rate_limit["limit"] == 2
        )
        await port.acquire(
            owner="other-owner", tool_name="library.start_audit", cost=1, arguments={}
        )
        await port.acquire(
            owner=OWNER, tool_name="confirm:library.start_audit", cost=1, arguments={}
        )
        assert not rows()
        backend.wake.assert_not_called()

    asyncio.run(run())


def test_native_cold_start_uses_same_owner_job_and_only_runs_original_worker_once(
    backend,
):
    async def run():
        store = SQLiteKernelStore()
        context = await context_for(store, owner=OWNER, session="audit-session")
        with pytest.raises(ValueError, match="确认"):
            sdk.library.start_audit(**ARGS)
        plan = await start_plan(store, context)
        assert plan.arguments == ARGS and not rows()
        assert plan.metadata["audit_job_snapshot"]["dedupe_active_id"] is None
        cold = effects(SQLiteKernelStore())
        pending = await cold.get_pending_plan(
            owner=context.owner, session_id=context.session_id
        )
        assert pending is not None and pending.plan_id == plan.plan_id
        for owner, session in [
            (OWNER, "foreign-session"),
            ("foreign-owner", "audit-session"),
        ]:
            foreign = await context_for(
                SQLiteKernelStore(), owner=owner, session=session
            )
            with pytest.raises(ToolPipelineError):
                await cold.execute_confirmed(plan.plan_id, context=foreign)
        result = (
            await cold.execute_confirmed(plan.plan_id, context=context)
        ).outcome.public_content
        identity = result["data"]["job_id"]
        assert (
            result["status"] == "accepted"
            and result["data"]["created"]
            and len(rows()) == 1
        )
        backend.wake.assert_called_once()
        with sdk.call_owner(OWNER):
            pending = sdk.library.audit_job(identity)
        assert (
            pending["status"] == "pending" and pending["data"]["progress_total"] is None
        )
        assert (
            pending["data"]["job_id"] == identity
            and not pending["data"]["coverage_complete"]
        )
        with pytest.raises(ToolPipelineError):
            await effects(SQLiteKernelStore()).execute_confirmed(
                plan.plan_id, context=context
            )
        assert len(rows()) == 1 and backend.run_once() == 1
        with sdk.call_owner(OWNER):
            done = sdk.library.audit_job(identity)
        assert done["status"] == "up_to_date" and done["data"]["coverage_complete"]
        assert done["data"]["progress_current"] == done["data"]["progress_total"] == 2
        assert backend.run_once() == 0
        with sdk.call_owner("foreign-owner"):
            assert sdk.library.audit_job(identity)["status"] == "not_run"

    asyncio.run(run())


def test_native_cancel_freezes_real_id_and_never_switches_to_newer_job(backend):
    async def run():
        store = SQLiteKernelStore()
        context = await context_for(store, owner=OWNER)
        _, snapshot = service.prepare_start_audit(owner=OWNER, **ARGS)
        first = service.start_audit(owner=OWNER, expected_snapshot=snapshot, **ARGS)[
            "data"
        ]["job_id"]
        result = await effects(store).prepare(
            "library.cancel_audit", {}, context=context
        )
        plan = result.effect_plan
        assert plan is not None and plan.arguments == {"job_id": first}
        second_args = {**ARGS, "max_series": 3}
        _, new_snapshot = service.prepare_start_audit(owner=OWNER, **second_args)
        second = service.start_audit(
            owner=OWNER, expected_snapshot=new_snapshot, **second_args
        )["data"]["job_id"]
        cancelled = await effects(SQLiteKernelStore()).execute_confirmed(
            plan.plan_id, context=context
        )
        assert cancelled.outcome.public_content["status"] == "completed"
        first_row = repository.get_agent_job(owner=OWNER, job_id=first)
        second_row = repository.get_agent_job(owner=OWNER, job_id=second)
        assert first_row is not None and first_row["status"] == "cancelled"
        assert second_row is not None and second_row["status"] == "pending"
        with sdk.call_owner(OWNER):
            bounded = sdk.library.audit_job(limit=1)["data"]
            assert len(bounded["jobs"]) == 1
            assert (
                bounded["scope"]["jobs_has_more"]
                and bounded["scope"]["jobs_total"] is None
            )
            complete = sdk.library.audit_job()["data"]
            assert {job["job_id"] for job in complete["jobs"]} == {first, second}
            assert (
                not complete["scope"]["jobs_has_more"]
                and complete["scope"]["jobs_total"] == 2
            )
            with pytest.raises(ValueError):
                sdk.library.audit_job(limit=0)
        with pytest.raises(ToolPipelineError):
            await effects(SQLiteKernelStore()).execute_confirmed(
                plan.plan_id, context=context
            )

    asyncio.run(run())


@pytest.mark.parametrize("cancel", [False, True])
def test_cancelled_confirmation_or_turn_cannot_enqueue(backend, cancel):
    async def run():
        store = SQLiteKernelStore()
        context = await context_for(store, owner=OWNER)
        plan = await start_plan(store, context)
        cold = effects(SQLiteKernelStore())
        if cancel:
            assert await cold.cancel_effect(plan.plan_id, lease=context.lease)
        else:
            context.cancellation.cancel()
        with pytest.raises((ToolPipelineError, asyncio.CancelledError)):
            await cold.execute_confirmed(plan.plan_id, context=context)
        assert not rows()
        backend.wake.assert_not_called()

    asyncio.run(run())


def test_native_cancel_running_job_stops_at_batch_boundary_and_preserves_prior_checkpoint(
    backend,
):
    entered = threading.Event()
    release = threading.Event()

    def batch(arguments):
        entered.set()
        assert release.wait(3)
        return _result(ok=True, status="up_to_date", checked=2)

    backend._audit_executor = Mock(side_effect=batch)

    async def run():
        store = SQLiteKernelStore()
        context = await context_for(store, owner=OWNER)
        plan = await start_plan(store, context)
        accepted = (
            await effects(store).execute_confirmed(plan.plan_id, context=context)
        ).outcome.public_content
        identity = accepted["data"]["job_id"]
        cancel = (
            await effects(store).prepare(
                "library.cancel_audit", {"job_id": identity}, context=context
            )
        ).effect_plan
        assert cancel is not None
        worker = asyncio.create_task(asyncio.to_thread(backend.run_once))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            result = (
                await effects(SQLiteKernelStore()).execute_confirmed(
                    cancel.plan_id, context=context
                )
            ).outcome.public_content
            assert result["status"] == "accepted" and result["data"]["cancel_requested"]
        finally:
            release.set()
            assert await worker == 1
        with sdk.call_owner(OWNER):
            result = sdk.library.audit_job(identity)
        assert (
            result["status"] == "cancelled" and result["data"]["progress_current"] == 0
        )
        assert not result["data"]["coverage_complete"] and backend.run_once() == 0
        backend._audit_executor.assert_called_once()

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["start_audit", "cancel_audit"])
def test_lost_receipt_never_replays_or_targets_a_replacement(
    backend, monkeypatch, operation
):
    async def run():
        store = SQLiteKernelStore()
        context = await context_for(store, owner=OWNER)
        if operation == "start_audit":
            plan = await start_plan(store, context)
        else:
            _, snapshot = service.prepare_start_audit(owner=OWNER, **ARGS)
            identity = service.start_audit(
                owner=OWNER, expected_snapshot=snapshot, **ARGS
            )["data"]["job_id"]
            plan = (
                await effects(store).prepare(
                    "library.cancel_audit", {"job_id": identity}, context=context
                )
            ).effect_plan
            assert plan is not None
        original = getattr(service, operation)
        calls = []

        def lost(**kwargs):
            value = original(**kwargs)
            calls.append(value)
            raise RuntimeError("lost private receipt")

        monkeypatch.setattr(service, operation, lost)
        with pytest.raises(ToolPipelineError) as caught:
            await effects(SQLiteKernelStore()).execute_confirmed(
                plan.plan_id, context=context
            )
        assert caught.value.execution_started is True and len(calls) == 1
        before = rows()
        with pytest.raises(ToolPipelineError):
            await effects(SQLiteKernelStore()).execute_confirmed(
                plan.plan_id, context=context
            )
        assert rows() == before and len(calls) == 1

    asyncio.run(run())


@pytest.mark.parametrize("corrupt", [False, True])
def test_stored_bounded_projection_preserves_aggregate_counts_and_unknown_detail_totals(
    backend, corrupt
):
    _, snapshot = service.prepare_start_audit(owner=OWNER, **ARGS)
    identity = service.start_audit(owner=OWNER, expected_snapshot=snapshot, **ARGS)[
        "data"
    ]["job_id"]
    projection = empty_patrol_projection(as_of=AS_OF)
    projection.update(
        patrol_status="updates_available",
        checked_series_count=3,
        updates_available_count=1,
        missing_episode_count=50,
        findings_truncated=True,
        options=[
            {
                "position": 1,
                "title": "Show",
                "tmdb_id": "123",
                "season": 1,
                "missing_count": 20,
                "episode_sample": list(range(1, 21)),
            }
        ],
    )
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE agent_jobs SET status='succeeded',projection_json=?,progress_current=3,progress_total=3 WHERE job_id=?",
            ("{}" if corrupt else json.dumps(projection), identity),
        )
    with sdk.call_owner(OWNER):
        result = sdk.library.audit_job(identity)
    data = result["data"]
    if corrupt:
        assert result["status"] == "inconclusive" and not data["projection_valid"]
        assert data["missing_episode_count"] is None and data["findings_total"] is None
        assert not data["coverage_complete"] and not data["findings_complete"]
    else:
        assert (
            result["status"] == "updates_available"
            and data["missing_episode_count"] == 50
        )
        assert data["findings_truncated"] and data["findings_total"] is None
        assert (
            len(data["findings"][0]["missing_sample"])
            == data["findings"][0]["missing_sample_count"]
            == 20
        )
        assert (
            data["findings"][0]["missing_count"] is None
            and data["findings"][0]["missing_sample_truncated"] is None
        )
        assert data["coverage_complete"] and not data["findings_complete"]


@pytest.mark.parametrize("language", ["python", "shell"])
def test_real_workspace_api_cli_freeze_cold_confirm_and_read_saved_job_file(
    backend, monkeypatch, tmp_path, language
):
    if not sandbox.sandbox_supported():
        pytest.skip(sandbox.workspace_unavailable_reason())
    monkeypatch.setattr(
        sandbox,
        "get_runtime_paths",
        lambda: SimpleNamespace(data_dir=tmp_path, runtime_dir=tmp_path / "runtime"),
    )

    async def run():
        store = SQLiteKernelStore()
        context = await context_for(
            store, owner=OWNER, session=f"audit-workspace-{language}"
        )
        native = effects(store)
        pipeline = ToolPipeline(
            catalog=native.catalog,
            state_store=store,
            reference_store=store,
            effect_store=native.effect_store,
        )
        pipeline.catalog.register(sandbox.run_code_tool(pipeline))
        code = (
            "from pathlib import Path\nfrom mf import library\n"
            'Path("before").write_text("once")\n'
            f"library.start_audit(as_of={AS_OF!r}, max_series=2)\n"
            'Path("after").write_text("unexpected")'
            if language == "python"
            else f"echo once > before\nmf library.start_audit --as-of {AS_OF} --max-series 2\necho unexpected > after"
        )
        try:
            prepared = await pipeline.execute(
                sandbox.RUN_CODE_TOOL,
                {"language": language, "code": code},
                context=context,
            )
            plan = prepared.effect_plan
            assert plan is not None and plan.tool_name == "library.start_audit", (
                prepared.outcome.public_content
            )
            assert plan.arguments == ARGS and not rows()
            root, _ = sandbox._workspace_location(context.owner, context.session_id)
            assert (root / "work/before").read_text().strip() == "once"
            assert not (root / "work/after").exists()
            cold = effects(SQLiteKernelStore())
            pending = await cold.get_pending_plan(
                owner=OWNER, session_id=context.session_id
            )
            assert pending is not None and pending.plan_id == plan.plan_id
            accepted = (
                await cold.execute_confirmed(plan.plan_id, context=context)
            ).outcome.public_content
            identity = accepted["data"]["job_id"]
            assert accepted["status"] == "accepted" and len(rows()) == 1
            backend.wake.assert_called_once()
            assert not (root / "work/after").exists()
            read = await pipeline.execute(
                sandbox.RUN_CODE_TOOL,
                {
                    "language": "shell",
                    "code": f"mf library.audit_job --job-id {identity} > job.json\ncat job.json",
                },
                context=context,
            )
            assert read.outcome.public_content["data"]["exit_code"] == 0, (
                read.outcome.public_content
            )
            saved = json.loads((root / "work/job.json").read_text())
            assert saved["data"]["job_id"] == identity and saved["status"] == "pending"
            assert (
                saved["data"]["progress_total"] is None
                and not saved["data"]["coverage_complete"]
            )
            cancel = await pipeline.execute(
                sandbox.RUN_CODE_TOOL,
                {
                    "language": "python",
                    "code": "from mf import library\n"
                    f'assert library.audit_job({identity!r})["data"]["job_id"]=={identity!r}\n'
                    f"library.cancel_audit({identity!r})",
                },
                context=context,
            )
            cancel_plan = cancel.effect_plan
            assert (
                cancel_plan is not None
                and cancel_plan.tool_name == "library.cancel_audit"
            ), cancel.outcome.public_content
            assert cancel_plan.arguments == {"job_id": identity}
            result = await effects(SQLiteKernelStore()).execute_confirmed(
                cancel_plan.plan_id, context=context
            )
            assert result.outcome.public_content["status"] == "completed"
            readback = await pipeline.execute(
                sandbox.RUN_CODE_TOOL,
                {
                    "language": "python",
                    "code": "from pathlib import Path\nimport json\nfrom mf import library\n"
                    f'Path("cancelled.json").write_text(json.dumps(library.audit_job({identity!r})))',
                },
                context=context,
            )
            assert readback.outcome.public_content["data"]["exit_code"] == 0, (
                readback.outcome.public_content
            )
            final = json.loads((root / "work/cancelled.json").read_text())
            assert (
                final["data"]["job_id"] == identity and final["status"] == "cancelled"
            )
            assert backend.run_once() == 0 and not (root / "work/after").exists()
            with pytest.raises(ToolPipelineError):
                await effects(SQLiteKernelStore()).execute_confirmed(
                    plan.plan_id, context=context
                )
        finally:
            sandbox.clear_workspace(owner=context.owner, session_id=context.session_id)

    asyncio.run(run())
