"""同步失败统一 effect 登记：真实 SQLite/确认票据，无模型和网络调用。"""
from __future__ import annotations

import asyncio
import json
import socket
import time
from copy import deepcopy
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import requests

import app.agent.kernel.completion_receipts as completion_receipts
from app import database as db
from app.agent import effect_completion as effects
from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.errors import ConfirmationClaimError, ToolPipelineError
from app.agent.kernel.bootstrap import _capture_creation_binding
from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.continuation import effect_origin, effect_payload
from app.agent.kernel.effects import ConfirmationEffectPlanStore
from app.agent.kernel.tool_calls import PreparedEffect
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.ports.mediaflux_effects import MediaFluxEffectLifecycle
from app.agent.kernel.projection import ToolOutcome
from app.agent.kernel.state_types import CancellationToken, StalePublicationError
from app.agent.kernel.tool_calls import ToolCallContext
from app.agent.models import ToolContext, ToolReference, ToolResult
from tests.support import initialized_web_runtime

OWNER, SID, TOOL = "sync-effect-owner", "sync-effect-session", "fixture.change"
GOAL = "修改本地测试文件，失败后重新读取核验。"
PRIVATE = "private-sync-source-identity"


class Interrupted(BaseException):
    pass


class RegistrationCase:
    def __init__(self, root):
        self.root = root
        self.clock = [time.time()]
        self.store = self.cold_store()
        self.options = {}
        self.executions = []
        self.failure = ToolPipelineError("本地写后核验失败", code="fixture_failed")
        self.source = {"version": 1, "kind": "fixture_files", "owner": OWNER, "session_id": SID,
                       "read": {"tool": "fixture.inspect", "arguments": {}},
                       "baseline": {"selected": [{"identity": PRIVATE}]}}

        def prepare(arguments, context):
            return PreparedEffect(preview={"summary": "等待确认"}, snapshot_fingerprint="local-state",
                                  metadata={"reobservation_source": deepcopy(self.source)})

        async def execute(arguments, fingerprint, context):
            self.executions.append(dict(arguments))
            (root / "write-fact.txt").write_text("实际调用过一次本地写入")
            if self.options.get("stop"):
                assert await self.store.request_stop_generation(owner=OWNER, session_id=SID, generation=context.lease.generation)
            mode = self.options.get("mode")
            if mode == "execute_error":
                raise self.failure
            if mode == "cancelled":
                raise asyncio.CancelledError()
            if mode == "interrupted":
                raise Interrupted()
            result = ToolResult(False, "fixture_failed", "本地写后核验失败", data={"observed": True})
            if mode == "tool_outcome":
                return ToolOutcome(model_content=json.dumps(result.to_dict()), public_content=result.to_dict())
            if mode == "reference":
                result.references = [ToolReference("fixture_state", {"private_identity": PRIVATE})]
            return result

        def verify(arguments, result, context):
            return False if self.options.get("mode") == "verify_error" else result

        self.tool = KernelToolSpec(name=TOOL, domain="fixture", description="本地失败确认",
            input_schema={"type": "object", "properties": {"fixture_blob_ref": {"type": "string"}}, "additionalProperties": False},
            effect=ToolEffect.WRITE, prepare=prepare, execute_confirmed=execute, verify=verify)
        self.pipeline = ToolPipeline(catalog=ToolCatalog([self.tool]), state_store=self.store, reference_store=self.store,
            effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore(clock=lambda: self.clock[0]), record_actions=True),
            effect_lifecycle=MediaFluxEffectLifecycle())

    def cold_store(self):
        return SQLiteKernelStore(secret_provider=lambda: "sync-registration-secret", clock=lambda: self.clock[0])

    async def preview(self, arguments=None):
        lease, _state = await self.store.begin_turn(owner=OWNER, session_id=SID, request_id="real-user-request")
        await self.store.commit(lease, conversation=[{"role": "user", "content": GOAL}])

        async def progress(payload):
            pass

        self.context = ToolCallContext(owner=OWNER, session_id=SID, request_id=lease.request_id,
            turn_id=lease.turn_id, lease=lease, cancellation=CancellationToken(), report_progress=progress,
            user_source=GOAL, channel="web", continuation_binding=_capture_creation_binding(),
            continuation_policy={"model_policy": "", "model_limit": 8, "tool_limit": 8})
        args = arguments or {}
        preview = await self.pipeline.execute(self.tool.name, args, context=self.context)
        assert preview.effect_plan is not None
        self.plan_id = preview.effect_plan.plan_id
        state = await self.store.load(owner=OWNER, session_id=SID)
        self.scope = completion_receipts.EffectCompletionScope(store=self.store, lease=lease, plan_id=self.plan_id, channel="web",
            origin=effect_origin(state, self.context, preview.effect_plan))
        # 原生调用/结果来自本次真实预检；不手造 claimed 标记或成功执行回执。
        await self.store.commit(lease, conversation=[*state.conversation,
            {"role": "assistant", "content": "", "tool_calls": [{"call_id": "preview-call", "name": self.tool.name, "arguments": args}]},
            {"role": "tool", "tool_call_id": "preview-call", "tool_name": self.tool.name,
             "effect_plan_id": self.plan_id, "content": preview.outcome.model_message()}])
        assert "task" not in state.metadata and "read_wait" not in state.metadata
        return preview

    async def confirm(self, *, defer_registration=False):
        if defer_registration:
            # 仅延后登记以独立验证事务；预检、claim、执行和失败结果仍跑真实管道。
            with patch.object(effects, "register_terminal_failure", new=AsyncMock(return_value=False)):
                return await self.pipeline.execute_confirmed(self.plan_id, context=self.context)
        return await self.pipeline.execute_confirmed(self.plan_id, context=self.context)

    async def state(self):
        return await self.cold_store().load(owner=OWNER, session_id=SID)


@pytest.fixture
def case(tmp_path, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("同步登记测试禁止真实网络/LLM/云盘")

    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    monkeypatch.setattr(requests.sessions.Session, "request", no_network)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_network)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_network)
    with initialized_web_runtime():
        current = RegistrationCase(tmp_path)
        monkeypatch.setattr(effects.time, "time", lambda: current.clock[0])
        yield current


@pytest.mark.parametrize("mode", ["returned", "tool_outcome", "reference", "execute_error", "verify_error", "ref_stale"])
def test_real_confirmation_persists_failure_receipt_and_cold_effect_without_task(case, mode):
    case.options["mode"] = mode

    async def run():
        args = {}
        if mode == "ref_stale":
            ref = await case.store.put(owner=OWNER, session_id=SID, kind="fixture_blob", value={"private": PRIVATE}, ttl_seconds=1)
            args = {"fixture_blob_ref": ref.ref}
        await case.preview(args)
        if mode == "ref_stale":
            case.clock[0] += 2
        if mode in {"execute_error", "verify_error", "ref_stale"}:
            with pytest.raises(ToolPipelineError) as caught:
                await case.confirm()
            code = {"execute_error": "fixture_failed", "verify_error": "post_write_verification_failed", "ref_stale": "confirmation_stale"}[mode]
            assert caught.value.code == code
            if mode == "execute_error":
                assert caught.value is case.failure
            assert caught.value.execution_started is (mode != "ref_stale")
            expected = {"ok": False, "status": code, "summary": str(caught.value),
                        "data": {"execution_started": mode != "ref_stale"}}
        else:
            result = await case.confirm()
            expected = dict(result.outcome.public_content)
            if mode == "reference":
                assert expected["reference_arguments"]["fixture_state_ref"].startswith("ref_")
        state = await case.state()
        row = state.metadata["effect_waits"][case.plan_id]
        assert row["state"] == "terminal" and row["final_result"] == expected
        assert row["continuation"]["state"] == "pending"
        assert row["continuation"]["kind"] == "effect" and row["continuation"]["expected_task"] is None
        assert "task" not in state.metadata and "read_wait" not in state.metadata
        receipts = [r for r in state.conversation if r.get("completion_receipt_id") == case.plan_id]
        assert receipts == [row["receipt_message"]]
        original_tool = next(r for r in state.conversation if r.get("tool_call_id") == "preview-call")
        assert json.loads(original_tool["content"]) == expected
        payload = effect_payload(state, row["continuation"])
        assert "tracker" not in payload["seed"]
        assert payload["origin"]["goal"] == {"role": "user", "content": GOAL}
        assert payload["origin"]["reobservation_source"] == case.source
        assert PRIVATE not in json.dumps(state.to_payload(), ensure_ascii=False)
        with pytest.raises(ConfirmationClaimError):
            await case.confirm()
        candidate, = await case.cold_store().due_continuations()
        assert PRIVATE not in json.dumps(candidate, ensure_ascii=False)
        assert await case.cold_store().claim_continuation(candidate, case.context.continuation_binding) is not None
        assert await case.cold_store().claim_continuation(candidate, case.context.continuation_binding) is None
        with pytest.raises(StalePublicationError):
            await case.confirm()
        assert len(case.executions) == (0 if mode == "ref_stale" else 1)
        with db.get_conn() as conn:
            assert conn.execute("SELECT COUNT(*) FROM organize_operation_jobs").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM agent_confirmations").fetchone()[0] == 0
    asyncio.run(run())


def test_registration_and_receipt_roll_back_together(case, monkeypatch):
    async def run():
        await case.preview()
        result = await case.confirm(defer_registration=True)
        before = await case.state()
        write = case.store._write_state

        def write_then_fail(conn, state):
            row = state.metadata["effect_waits"][case.plan_id]
            assert row["continuation"]["state"] == "pending"
            assert row["receipt_message"] in state.conversation
            write(conn, state)
            raise RuntimeError("after SQL write, before commit")

        with monkeypatch.context() as current:
            current.setattr(case.store, "_write_state", write_then_fail)
            with pytest.raises(RuntimeError, match="before commit"):
                await effects.register_terminal_failure(case.scope, tool=TOOL, result=result.outcome.public_content)
        assert await case.state() == before
        assert await case.cold_store().due_continuations() == []
        assert await effects.register_terminal_failure(case.scope, tool=TOOL, result=result.outcome.public_content)
        row = (await case.state()).metadata["effect_waits"][case.plan_id]
        assert row["final_result"] == result.outcome.public_content
    asyncio.run(run())


def test_duplicate_registration_preserves_budget_deadline_and_richer_receipt(case):
    async def run():
        await case.preview()
        result = await case.confirm()

        def spend(state):
            record = state.metadata["effect_waits"][case.plan_id]["continuation"]
            record["budget"].update(model_spent=2, tool_spent=1, wait_segments=1)
        await case.store.update_effect_state(owner=OWNER, session_id=SID, change=spend)
        row = (await case.state()).metadata["effect_waits"][case.plan_id]
        message = {**row["receipt_message"], "candidate_result_ref": "ref_receipt_enrichment"}
        assert await completion_receipts.remember_effect_receipt(case.store, owner=OWNER, session_id=SID, plan_id=case.plan_id, message=message)
        before = await case.state()
        case.clock[0] += 5
        assert await effects.register_terminal_failure(case.scope, tool=TOOL, result=result.outcome.public_content)
        assert await case.state() == before
        assert not await effects.register_terminal_failure(case.scope, tool=TOOL, result={**result.outcome.public_content, "summary": "改写失败事实"})
        assert await case.state() == before
        receipts = [r for r in before.conversation if r.get("completion_receipt_id") == case.plan_id]
        assert receipts == [message]
        assert await completion_receipts.acknowledge_effect_receipt(case.store, owner=OWNER, session_id=SID, plan_id=case.plan_id)
        after = await case.state()
        assert after.metadata["effect_waits"][case.plan_id]["delivered"] is True
        assert after.metadata["effect_waits"][case.plan_id]["continuation"] == row["continuation"]
    asyncio.run(run())


@pytest.mark.parametrize("field", ["owner", "session_id", "generation", "turn_id", "plan_id"])
def test_wrong_confirmation_scope_cannot_register(case, field):
    async def run():
        await case.preview()
        result = await case.confirm(defer_registration=True)
        before = await case.state()
        scope = case.scope
        origin = deepcopy(scope.origin)
        if field == "plan_id":
            scope = replace(scope, plan_id="another-plan")
        else:
            value = scope.lease.generation + 1 if field == "generation" else "other-scope"
            key = {"generation": "origin_generation", "turn_id": "source_turn_id"}.get(field, field)
            origin[key] = value
            if field in {"owner", "session_id"}:
                origin["reobservation_source"][field] = value
            scope = replace(scope, lease=replace(scope.lease, **{field: value}), origin=origin)
        try:
            registered = await effects.register_terminal_failure(scope, tool=TOOL, result=result.outcome.public_content)
        except StalePublicationError:
            registered = False
        assert registered is False
        assert await case.state() == before
    asyncio.run(run())


@pytest.mark.parametrize("fault", ["unclaimed", "origin", "goal", "source_scope", "policy"])
def test_unclaimed_or_untrusted_origin_never_registers(case, fault):
    async def run():
        await case.preview()
        if fault == "unclaimed":
            result = {"ok": False, "status": "fixture_failed", "summary": "不是一次真实确认失败"}
        else:
            result = (await case.confirm(defer_registration=True)).outcome.public_content
        before = await case.state()
        scope = case.scope
        origin = deepcopy(scope.origin)
        if fault == "origin":
            origin = None
        elif fault == "goal":
            origin["goal"] = {"role": "assistant", "content": "模型自造目标"}
        elif fault == "source_scope":
            origin["reobservation_source"]["session_id"] = "foreign"
        elif fault == "policy":
            origin.pop("model_limit")
        try:
            registered = await effects.register_terminal_failure(replace(scope, origin=origin), tool=TOOL, result=result)
        except StalePublicationError:
            registered = False
        assert registered is False and await case.state() == before
    asyncio.run(run())


@pytest.mark.parametrize("fault", ["authorization_denied", "authorization_failed", "agent_disabled", "tool_not_found", "confirmation_invalid"])
def test_rejected_authority_or_tool_never_registers(case, fault):
    async def run():
        await case.preview()
        if fault == "tool_not_found":
            case.pipeline.catalog._tools.pop(TOOL)
        elif fault == "confirmation_invalid":
            case.pipeline.catalog._tools[TOOL] = replace(case.tool, effect=ToolEffect.DANGER)
        else:
            class Deny:
                async def authorize(self, *args):
                    raise ToolPipelineError("真实 authorize 拒绝", code=fault)
            case.pipeline.authorization = Deny()
        with pytest.raises(ToolPipelineError) as caught:
            await case.confirm()
        assert caught.value.code == fault
        assert not (await case.state()).metadata.get("effect_waits")
        assert case.executions == [] and await case.cold_store().due_continuations() == []
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["cancelled", "interrupted", "stop"])
def test_cancel_interruption_or_stop_cannot_create_a_diagnostic_effect(case, mode):
    async def run():
        await case.preview()
        if mode == "stop":
            case.options["stop"] = True
            await case.confirm()
        else:
            case.options["mode"] = mode
            with pytest.raises(asyncio.CancelledError if mode == "cancelled" else Interrupted):
                await case.confirm()
        assert not (await case.state()).metadata.get("effect_waits")
        assert await case.cold_store().due_continuations() == []
    asyncio.run(run())


@pytest.mark.parametrize("status", ["accepted", "submitted", "running", "pending", "cancelled", "authorization_required"])
def test_nonterminal_or_forbidden_failure_dto_is_not_registered(case, status):
    async def run():
        await case.preview()
        result = (await case.confirm(defer_registration=True)).outcome.public_content
        before = await case.state()
        assert not await effects.register_terminal_failure(case.scope, tool=TOOL, result={**result, "status": status})
        assert await case.state() == before
    asyncio.run(run())


def test_registration_storage_fault_keeps_original_exception_and_code(case):
    async def run():
        await case.preview()
        case.options["mode"] = "execute_error"
        with patch.object(case.store, "update_effect_state", new=AsyncMock(side_effect=RuntimeError("storage unavailable"))):
            with pytest.raises(ToolPipelineError) as caught:
                await case.confirm()
        assert caught.value is case.failure and caught.value.code == "fixture_failed"
        assert not (await case.state()).metadata.get("effect_waits")
    asyncio.run(run())


def test_real_admitted_tracker_cannot_be_overwritten_by_sync_failure(case):
    from tests.test_agent_effect_admission import (
        _domain_arguments,
        _offline_runtime,
        _OfflineClient,
        _paused_manager,
        _spec,
    )

    async def run():
        client = _OfflineClient()
        with _offline_runtime(case.root, client) as stack, _paused_manager():
            arguments = _domain_arguments("fs", client, ToolContext(owner=OWNER, session_id=SID), stack)
            case.tool = _spec("fs")
            case.pipeline.catalog.register(case.tool)
            await case.preview(arguments)
            accepted = await case.confirm()
            assert accepted.outcome.public_content["status"] == "accepted"
            before = await case.state()
            row = before.metadata["effect_waits"][case.plan_id]
            payload = json.loads(completion_receipts._completion_cipher().decrypt(row["sealed"].encode()))
            assert payload["tracker"]["kind"] == "guangya_operation"
            with db.get_conn() as conn:
                assert conn.execute("SELECT COUNT(*) FROM organize_operation_jobs WHERE status='pending'").fetchone()[0] == 1
            assert not await effects.register_terminal_failure(case.scope, tool=case.tool.name,
                result={"ok": False, "status": "fixture_failed", "summary": "前台错误不得覆盖真实已受理任务"})
            assert await case.state() == before
            assert row["state"] == "pending" and "final_result" not in row
    asyncio.run(run())
