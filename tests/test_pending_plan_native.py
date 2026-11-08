"""原生会话票据控制：复用真实Kernel、SQLite、原生RPC/CLI及现有票据夹具。"""

from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app import sdk
from app.agent.errors import ToolPipelineError
from app.agent.kernel import sandbox
from app.agent.kernel.session_service import SessionService
from app.agent.kernel.state_types import CancellationToken, StalePublicationError
from app.agent.kernel.tool_calls import ToolCallContext
from tests import test_agent_pending_dialogue as dialogue


@pytest.fixture
def rig(monkeypatch, tmp_path):
    case = dialogue.PendingDialogueTests()
    case.setUp()
    monkeypatch.setattr(
        sandbox,
        "get_runtime_paths",
        lambda: SimpleNamespace(data_dir=tmp_path, runtime_dir=tmp_path / "runtime"),
    )
    try:
        yield case
    finally:
        case.doCleanups()


async def context_for(runtime, owner=dialogue.OWNER, session_id=dialogue.SID):
    pipeline = runtime.pipeline
    state = await runtime.state_store.load(owner=owner, session_id=session_id)
    plan = await pipeline.get_pending_plan(owner=owner, session_id=session_id)
    guard = pipeline.pending_guard(state, plan) if plan is not None else None
    lease, _ = await runtime.state_store.begin_turn(
        owner=owner,
        session_id=session_id,
        request_id="native-control",
        pending_guard=guard,
    )

    async def progress(_):
        pass

    return ToolCallContext(
        owner=owner,
        session_id=session_id,
        request_id=lease.request_id,
        turn_id=lease.turn_id,
        lease=lease,
        cancellation=CancellationToken(),
        report_progress=progress,
    )


async def code(runtime, context, source):
    return await runtime.pipeline.execute(
        sandbox.RUN_CODE_TOOL, {"code": source}, context=context
    )


def test_session_binding_and_retired_model_tool(rig):
    assert not rig.session.catalog.has("agent.pending_plan")
    assert (
        "session.pending_plan" in sdk.METHODS
        and "session.pending_plan" not in sdk.WRITE_METHODS
    )
    assert list(inspect.signature(sdk.session.pending_plan).parameters) == ["operation"]
    window = rig.session.visible_model_tools({"has_pending_effect": True})
    assert "agent.pending_plan" not in {tool.name for tool in window}
    with pytest.raises(ValueError, match="宿主绑定"):
        sdk.session.pending_plan()
    callback = Mock(return_value={"ok": True})
    with sdk.call_owner("first", session_call=callback):
        assert sdk.session.pending_plan() == {"ok": True}
        callback.assert_called_once_with("pending_plan", {"operation": "inspect"})
        with sdk.call_owner("second"), pytest.raises(ValueError, match="宿主绑定"):
            sdk.session.pending_plan("cancel")
        sdk.session.pending_plan("cancel")
        callback.assert_called_with("pending_plan", {"operation": "cancel"})


@pytest.mark.parametrize(
    "operation", ["confirm", "execute", "undo", "", None, True, {}, []]
)
def test_invalid_operation_never_changes_existing_ticket(rig, operation):
    async def run():
        original = await rig.preview()
        ctx = await context_for(rig.session)
        service = SessionService(rig.session.pipeline, ctx)
        with pytest.raises(ToolPipelineError) as failed:
            await service.pending_plan(operation)
        assert failed.value.code == "invalid_arguments"
        assert await rig.pending() == original and rig.rows() == []

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["inspect", "cancel"])
def test_stale_lease_and_cancelled_call_preserve_ticket(rig, operation):
    async def run():
        original = await rig.preview()
        stale = await context_for(rig.session)
        current = await context_for(rig.session)
        with pytest.raises(StalePublicationError):
            await SessionService(rig.session.pipeline, stale).pending_plan(operation)
        current.cancellation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await SessionService(rig.session.pipeline, current).pending_plan(operation)
        assert await rig.pending() == original and rig.rows() == []

    asyncio.run(run())


def test_native_inspect_cli_cancel_and_repeat_keep_original_ticket_contract(rig):
    async def run():
        original = await rig.preview()
        context = await context_for(rig.session)
        effects = rig.session.pipeline.effect_store
        with patch.object(effects, "cancel", wraps=effects.cancel) as cancel:
            result = await code(
                rig.session,
                context,
                """import json, subprocess
from mf import session
before=session.pending_plan()
cli=json.loads(subprocess.check_output(["mf","session.pending_plan","--operation","inspect"]))
assert before==cli and before["data"]["pending"]
json.dump(before,open("original-ticket.json","w"))
cancelled=session.pending_plan(operation="cancel")
again=json.loads(subprocess.check_output(["mf","session.pending_plan","--operation","cancel"]))
assert cancelled["data"]=={"pending":False,"cancelled":True}
assert again["data"]=={"pending":False,"cancelled":False}
print(json.dumps(before,ensure_ascii=False))
""",
            )
            data = result.outcome.public_content["data"]
            assert data["exit_code"] == 0, data
            assert (
                json.loads(data["stdout"])["data"]["preview"] == original.public_result
            )
            assert [call["method"] for call in data["sdk_calls"]] == [
                "session.pending_plan"
            ] * 4
            assert all(call["ok"] for call in data["sdk_calls"])
            assert result.effect_plan is None
            cancel.assert_called_once_with(
                owner=dialogue.OWNER,
                session_id=dialogue.SID,
                generation=original.generation,
                plan_id=original.plan_id,
            )
        assert rig.rows() == [] and await rig.pending() is None
        rig.rejected(await rig.confirm(original))

    asyncio.run(run())


def test_native_cancel_then_fresh_preflight_preserves_new_confirmation_receipt(rig):
    async def run():
        original = await rig.preview()
        pipeline = rig.session.pipeline
        native_write = replace(
            rig.session.catalog.get(dialogue.WRITE),
            name="rss.create",
            model_name="rss__create",
        )
        rig.session.catalog.register(native_write)
        context = await context_for(rig.session)
        with patch.dict(sandbox.METHODS, {"rss.create": lambda name: None}):
            result = await code(
                rig.session,
                context,
                """from mf import session, rss
assert session.pending_plan("cancel")["data"]["cancelled"]
rss.create(name="replacement")
raise AssertionError("预检后必须等待原确认入口")
""",
            )
        assert result.effect_plan is not None
        replacement = result.effect_plan
        assert replacement.plan_id != original.plan_id and replacement.arguments == {
            "name": "replacement"
        }
        assert result.outcome.public_content["status"] == "approval_required"
        assert rig.rows() == []
        assert rig.trace[-2][0] == "cancel" and rig.trace[-1] == (
            "prepare",
            "replacement",
            None,
        )
        rig.rejected(await rig.confirm(original))
        assert (
            await pipeline.get_pending_plan(
                owner=context.owner, session_id=context.session_id
            )
        ).plan_id == replacement.plan_id
        confirmed = await rig.confirm(replacement)
        assert (
            sum(event.type == dialogue.E.EFFECT_COMPLETED for event in confirmed) == 1
        )
        assert rig.rows() == ["replacement"]
        rig.rejected(await rig.confirm(replacement))
        assert rig.rows() == ["replacement"]

    asyncio.run(run())


def test_executing_confirmation_cannot_be_cancelled_or_replayed(rig):
    from app.agent.kernel.state_types import SessionBusyError

    async def run():
        original = await rig.preview()
        pipeline = rig.session.pipeline
        context = await context_for(rig.session)
        entered, release = asyncio.Event(), asyncio.Event()
        tool = rig.session.catalog.get(dialogue.WRITE)

        async def executing(arguments, snapshot, bound):
            entered.set()
            await release.wait()
            return tool.execute_confirmed(arguments, snapshot, bound)

        rig.session.catalog._tools[dialogue.WRITE] = replace(
            tool, execute_confirmed=executing
        )
        running = asyncio.create_task(
            pipeline.execute_confirmed(original.plan_id, context=context)
        )
        try:
            await asyncio.wait_for(entered.wait(), 2)
            with pytest.raises(SessionBusyError):
                await SessionService(pipeline, context).pending_plan("cancel")
            assert not any(row[0] == "cancel" for row in rig.trace)
        finally:
            release.set()
        receipt = await running
        assert receipt.outcome.public_content["ok"]
        assert rig.rows() == ["original"]
        after = await SessionService(pipeline, context).pending_plan("cancel")
        assert after["data"] == {"pending": False, "cancelled": False}
        assert rig.rows() == ["original"]

    asyncio.run(run())
