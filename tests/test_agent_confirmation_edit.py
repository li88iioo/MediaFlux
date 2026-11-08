"""可编辑卡走真实 SQLite 票据与原 pipeline；领域副作用限于内存。"""

import asyncio
import json
import os
from dataclasses import replace
from unittest.mock import patch

import pytest

from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.errors import ConfirmationClaimError, ToolPipelineError
from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.effects import ConfirmationEffectPlanStore
from app.agent.kernel.tool_calls import PreparedEffect
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.state_types import CancellationToken
from app.agent.kernel.tool_calls import ToolCallContext
from tests.test_agent_task_cas import task_store as isolated_store

task_store = isolated_store
OWNER, SESSION = "edit-owner", "confirmation-edit-session"


class EditRig:
    def __init__(self, store):
        self.now = 2_000_000_000.0
        self.prepares, self.writes = [], []
        self.fail_prepare = False
        self.value = "before"

        def prepare(arguments, _context):
            self.prepares.append(dict(arguments))
            if self.fail_prepare:
                raise ToolPipelineError("目标预检失败", code="precondition_failed")
            return PreparedEffect(preview={"ok": True, "summary": f"修改名称为 {arguments['label']}"},
                                  arguments={**arguments, "frozen_internal_id": "private-object-9"},
                                  snapshot_fingerprint=json.dumps([self.value, arguments], sort_keys=True))

        def execute(arguments, fingerprint, _context):
            expected = json.dumps([self.value, {k: v for k, v in arguments.items() if k != "frozen_internal_id"}], sort_keys=True)
            if fingerprint != expected:
                raise ToolPipelineError("目标变化", code="confirmation_stale")
            assert arguments["frozen_internal_id"] == "private-object-9"
            self.writes.append(dict(arguments))
            self.value = arguments["label"]
            return {"ok": True, "status": "completed", "summary": "名称已修改"}

        self.tool = KernelToolSpec(name="test.edit", domain="test", description="修改名称", effect=ToolEffect.WRITE,
            input_schema={"type": "object", "required": ["label", "object_id", "signature"], "additionalProperties": False,
                          "properties": {"label": {"type": "string", "minLength": 1, "maxLength": 40},
                                         "enabled": {"type": "boolean"}, "positions": {"type": "array", "items": {"type": "integer"}, "maxItems": 3},
                                         "object_id": {"type": "string"}, "signature": {"type": "string", "writeOnly": True}}},
            prepare=prepare, execute_confirmed=execute)
        self.pipeline = ToolPipeline(catalog=ToolCatalog([self.tool]), state_store=store,
            effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore(clock=lambda: self.now)))
        self.store = store

    async def context(self, *, owner=OWNER, session_id=SESSION):
        lease, _ = await self.store.begin_turn(owner=owner, session_id=session_id, request_id="edit-test")
        async def progress(_payload):
            pass
        return ToolCallContext(owner, session_id, lease.request_id, lease.turn_id, lease, CancellationToken(), progress)

    async def preview(self, context):
        return await self.pipeline.execute(self.tool.name,
            {"label": "original", "object_id": "opaque-object-7", "signature": "credential-signed-original"}, context=context)


@pytest.fixture
def rig(task_store, monkeypatch):
    monkeypatch.setattr("app.modules.web_secret.get_web_secret", lambda: "confirmation-edit-test-only")
    return EditRig(task_store)


def test_schema_patch_refreezes_without_exposing_or_replacing_internal_fields(rig):
    async def run():
        context = await rig.context()
        original = (await rig.preview(context)).effect_plan
        public = original.public_dict()
        assert set(public["editing"]["schema"]["properties"]) == {"label", "enabled", "positions"}
        assert public["editing"]["values"] == {"label": "original"}
        assert not any(value in json.dumps(public) for value in ("opaque-object-7", "credential-signed-original", "private-object-9"))
        rig.now += 30
        edited = await rig.pipeline.edit_confirmation(original.plan_id, {"label": "edited", "enabled": True}, context=context)
        assert rig.writes == [] and len(rig.prepares) == 2
        assert edited.effect_plan.plan_id != original.plan_id
        assert edited.effect_plan.expires_at <= original.expires_at + .01
        assert edited.effect_plan.arguments["object_id"] == "opaque-object-7"
        assert edited.effect_plan.arguments["signature"] == "credential-signed-original"
        with pytest.raises(ConfirmationClaimError):
            await rig.pipeline.execute_confirmed(original.plan_id, context=context)
        await rig.pipeline.execute_confirmed(edited.effect_plan.plan_id, context=context)
        assert rig.value == "edited" and len(rig.writes) == 1
        with pytest.raises(ConfirmationClaimError):
            await rig.pipeline.execute_confirmed(edited.effect_plan.plan_id, context=context)
        assert len(rig.writes) == 1
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["mutation", "effect"])
def test_pending_card_read_does_not_conflict_with_another_tasks_write_guard(rig, kind):
    from app.agent.kernel.session_guard import session_scope_guard

    async def run():
        context = await rig.context()
        plan = (await rig.preview(context)).effect_plan
        entered, release = asyncio.Event(), asyncio.Event()

        async def holding_write():
            with session_scope_guard(OWNER, SESSION, kind=kind):
                entered.set()
                await release.wait()

        holder = asyncio.create_task(holding_write())
        await entered.wait()
        try:
            observed = await rig.pipeline.get_pending_plan(owner=OWNER, session_id=SESSION)
            assert observed.plan_id == plan.plan_id
            assert rig.writes == []
        finally:
            release.set()
            await holder
        await rig.pipeline.execute_confirmed(plan.plan_id, context=context)
        assert len(rig.writes) == 1
        assert await rig.pipeline.get_pending_plan(owner=OWNER, session_id=SESSION) is None
        with pytest.raises(ConfirmationClaimError):
            await rig.pipeline.execute_confirmed(plan.plan_id, context=context)
        assert len(rig.writes) == 1

    asyncio.run(run())


@pytest.mark.parametrize("patch", [{"object_id": "replacement"}, {"signature": "replacement"}, {"unknown": 1},
                                    {"label": ""}, {"enabled": 1}, {"positions": [1, 2, 3, 4]}, {}])
def test_invalid_edit_revokes_old_card_and_cannot_execute(rig, patch):
    async def run():
        context = await rig.context()
        plan = (await rig.preview(context)).effect_plan
        with pytest.raises(ToolPipelineError) as error:
            await rig.pipeline.edit_confirmation(plan.plan_id, patch, context=context)
        assert error.value.plan_invalidated
        assert await rig.pipeline.get_pending_plan(owner=OWNER, session_id=SESSION) is None
        with pytest.raises(ConfirmationClaimError):
            await rig.pipeline.execute_confirmed(plan.plan_id, context=context)
        assert len(rig.prepares) == 1 and rig.writes == []
    asyncio.run(run())


def test_failed_repreflight_has_no_usable_ticket_or_business_write(rig):
    async def run():
        context = await rig.context()
        plan = (await rig.preview(context)).effect_plan
        rig.fail_prepare = True
        with pytest.raises(ToolPipelineError) as error:
            await rig.pipeline.edit_confirmation(plan.plan_id, {"label": "edited"}, context=context)
        assert error.value.code == "precondition_failed" and error.value.plan_invalidated
        assert rig.writes == [] and len(rig.prepares) == 2
        assert await rig.pipeline.get_pending_plan(owner=OWNER, session_id=SESSION) is None
        with pytest.raises(ConfirmationClaimError):
            await rig.pipeline.execute_confirmed(plan.plan_id, context=context)
    asyncio.run(run())


@pytest.mark.parametrize("delay", [600, 601])
def test_expired_card_cannot_be_edited_or_renewed(rig, delay):
    async def run():
        context = await rig.context()
        plan = (await rig.preview(context)).effect_plan
        rig.now = plan.expires_at + delay
        with pytest.raises(ConfirmationClaimError):
            await rig.pipeline.edit_confirmation(plan.plan_id, {"label": "edited"}, context=context)
        assert len(rig.prepares) == 1 and rig.writes == []
    asyncio.run(run())


@pytest.mark.parametrize("owner,sid", [("foreign-owner", SESSION), (OWNER, "foreign-session")])
def test_foreign_edit_cannot_invalidate_owner_card(rig, owner, sid):
    async def run():
        context = await rig.context()
        plan = (await rig.preview(context)).effect_plan
        foreign = await rig.context(owner=owner, session_id=sid)
        with pytest.raises(ConfirmationClaimError):
            await rig.pipeline.edit_confirmation(plan.plan_id, {"label": "edited"}, context=foreign)
        assert (await rig.pipeline.get_pending_plan(owner=OWNER, session_id=SESSION)).plan_id == plan.plan_id
        await rig.pipeline.execute_confirmed(plan.plan_id, context=context)
        assert rig.value == "original" and len(rig.writes) == 1
    asyncio.run(run())


def test_changed_domain_snapshot_after_edit_is_rejected_by_original_executor(rig):
    async def run():
        context = await rig.context()
        plan = (await rig.preview(context)).effect_plan
        edited = await rig.pipeline.edit_confirmation(plan.plan_id, {"label": "edited"}, context=context)
        rig.value = "external-change"
        with pytest.raises(ToolPipelineError) as error:
            await rig.pipeline.execute_confirmed(edited.effect_plan.plan_id, context=context)
        assert error.value.code == "confirmation_stale" and rig.writes == []
    asyncio.run(run())


@patch.dict(os.environ)
def test_real_preference_edit_confirm_and_next_query_preserve_auditable_history(task_store, tmp_path, monkeypatch):
    """真实本地配置执行器，只有模型按脚本；600旧调用保留，900成为实际UI意图和回执。"""
    from app import config
    from app.agent.feature_gate import initialize_runtime_fence

    from app.agent.kernel.events import AgentEventType as E
    from app.agent.kernel.model import ModelEvent, ModelToolCall
    from app.agent.kernel.model import ModelEventType as M
    from app.agent.kernel.ports.sdk_effects import policy_config_tool
    from app.agent.kernel.session import AgentSession
    from app.agent.kernel.state_types import AgentInput
    from tests.test_agent_kernel_core import ScriptedModel, collect

    env_file = tmp_path / "isolated-preferences.env"
    config.write_env_file(env_file, {"DISCOVERY_CACHE_TTL_SECONDS": "21600", "DISCOVERY_STALE_TTL_SECONDS": "604800"}, replace=False)
    monkeypatch.setattr(config, "ENV_FILE", env_file)
    monkeypatch.setattr(config, "_cache", None)
    monkeypatch.setattr(config, "_STARTUP_ENV_OVERRIDES", frozenset())
    monkeypatch.setattr("app.modules.web_secret.get_web_secret", lambda: "edit-history-only")
    for key in ("DISCOVERY_CACHE_TTL_SECONDS", "DISCOVERY_STALE_TTL_SECONDS"):
        monkeypatch.delenv(key, raising=False)
    initialize_runtime_fence()
    tool = policy_config_tool()
    from copy import deepcopy
    from dataclasses import replace
    origins = []
    original_execute = tool.execute_confirmed
    def execute(arguments, snapshot, context):
        origins.append(deepcopy(context.completion_scope.origin))
        assert original_execute is not None
        return original_execute(arguments, snapshot, context)
    tool = replace(tool, execute_confirmed=execute)
    catalog = ToolCatalog([tool])
    arguments = {"policy": "discovery_cache_ttl_seconds", "value": 600}
    final = [ModelEvent(M.TEXT_DELTA, text="以服务端实际回执为准。"), ModelEvent(M.FINISH, finish_reason="stop")]
    model = ScriptedModel([[ModelEvent(M.TOOL_CALL_COMPLETED, tool_call=ModelToolCall("preference-call", tool.name, arguments)),
                            ModelEvent(M.FINISH, finish_reason="tool_calls")], final, final])
    pipeline = ToolPipeline(catalog=catalog, state_store=task_store,
                            effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore()))
    session = AgentSession(model=model, catalog=catalog,  pipeline=pipeline, state_store=task_store)
    from app.agent.kernel.bootstrap import _capture_creation_binding
    session.continuation_binding = _capture_creation_binding()

    async def run():
        events = await collect(session.run(AgentInput(message="将影视探索缓存有效期改成600秒，先生成确认卡。", owner=OWNER,
                                                     session_id=SESSION, request_id="preference-preview")))
        old_id = next(event.payload["plan"]["plan_id"] for event in events if event.type == E.EFFECT_APPROVAL_REQUIRED)
        initial = await task_store.load(owner=OWNER, session_id=SESSION)
        source_users = [row for row in initial.conversation if row.get("role") == "user"]
        assert len(source_users) == 1 and source_users[0]["history_id"]
        assert source_users == [{"role": "user", "content": "将影视探索缓存有效期改成600秒，先生成确认卡。",
                                 "history_id": source_users[0]["history_id"], "history_order": 0}]
        result = await session.edit_confirmation(owner=OWNER, session_id=SESSION, plan_id=old_id,
                                                arguments={"value": 900}, request_id="actual-ui-edit")
        plan = result.effect_plan
        assert plan is not None and plan.arguments == {**arguments, "value": 900}
        state = await task_store.load(owner=OWNER, session_id=SESSION)
        edit = state.conversation[-1]
        assert edit["role"] == "user" and edit["ui_edit"]["arguments"]["value"] == 900
        assert edit["ui_edit"]["request_id"] == "actual-ui-edit"
        # 目标快照在提交前冻结；用户事件序号由持久化提交统一补充。
        assert edit == {**state.metadata["pending_effect_goal"], "history_order": 1}
        assert edit["history_id"] and edit["history_id"] != source_users[0]["history_id"]
        assert [row for row in state.conversation if row.get("role") == "user"] == source_users + [edit]
        old_call = next(call for row in state.conversation for call in row.get("tool_calls", ()))
        assert old_call["arguments"]["value"] == 600  # 不编造模型曾调用900。
        old_result = next(row for row in state.conversation if row.get("effect_plan_id") == old_id)
        assert old_result["effect_cancelled"] is True
        assert config.get_int("DISCOVERY_CACHE_TTL_SECONDS") == 21600
        stale = await collect(session.confirm(owner=OWNER, session_id=SESSION, plan_id=old_id, request_id="old-card"))
        assert stale[-1].type == E.TURN_FAILED
        assert config.get_int("DISCOVERY_CACHE_TTL_SECONDS") == 21600
        done = await collect(session.confirm(owner=OWNER, session_id=SESSION, plan_id=plan.plan_id, request_id="new-card"))
        assert any(event.type == E.EFFECT_COMPLETED for event in done), [(event.type, dict(event.payload)) for event in done if event.type in {E.EFFECT_FAILED, E.TURN_FAILED}]
        receipt = next(event for event in done if event.type == E.EFFECT_COMPLETED)
        assert "900" in receipt.payload["receipt"] and "600" not in receipt.payload["receipt"]
        assert config.get_int("DISCOVERY_CACHE_TTL_SECONDS") == 900
        await collect(session.run(AgentInput(message="刚才缓存有效期最终设为多少？", owner=OWNER,
                                            session_id=SESSION, request_id="after-edit-query")))
        messages = [message.to_dict() for message in model.requests[-1].messages]
        assert any(row["role"] == "user" and '"value":900' in row.get("content", "") for row in messages)
        assert any("900" in row.get("content", "") and "已将" in row.get("content", "") for row in messages)
        state = await task_store.load(owner=OWNER, session_id=SESSION)
        users = [row for row in state.conversation if row.get("role") == "user"]
        assert len(users) == 3 and users[-1]["history_id"] not in {source_users[0]["history_id"], edit["history_id"]}
        assert users == source_users + [
            {"role": "user", "content": edit["content"], "history_id": edit["history_id"], "history_order": 1},
            {"role": "user", "content": "刚才缓存有效期最终设为多少？", "history_id": users[-1]["history_id"], "history_order": 2},
        ]
        calls = [call for row in state.conversation for call in row.get("tool_calls", ())]
        assert len(calls) == 1 and calls[0]["arguments"]["value"] == 600
        assert len(origins) == 1
        assert origins[0]["goal"]["ui_edit"]["arguments"]["value"] == 900
        assert origins[0]["arguments"]["value"] == 900
    asyncio.run(run())


PRIVATE_EDIT_VALUES = {"access_code": "Ab12Cd34", "auto_access_code": False, "share_code": "Pq56Rs78",
                       "extraction_code": "Tt12Uu34", "passcode": "Vv56Ww78", "pin": "1234",
                       "accessCode": "Xx12Yy34", "share-code": "Zz56Aa78"}


def with_private_fields(rig):
    properties = {**rig.tool.input_schema["properties"], **{key: {"type": "boolean" if type(value) is bool else "string"}
                  for key, value in PRIVATE_EDIT_VALUES.items()}}
    rig.tool = replace(rig.tool, input_schema={**rig.tool.input_schema, "properties": properties})
    rig.pipeline.catalog = ToolCatalog([rig.tool])
    return {"label": "original", "object_id": "opaque-object-7", "signature": "credential-signed-original", **PRIVATE_EDIT_VALUES}


def test_edit_contract_excludes_short_credentials_from_schema_values_and_cold_plan_but_preserves_execution(rig):
    async def run():
        arguments = with_private_fields(rig)
        context = await rig.context()
        original = (await rig.pipeline.execute(rig.tool.name, arguments, context=context)).effect_plan
        cold = ConfirmationEffectPlanStore(SQLiteConfirmationStore(clock=lambda: rig.now)).get_active_plan(
            owner=OWNER, session_id=SESSION, generation=context.lease.generation, plan_id=original.plan_id)
        assert cold is not None
        for public in (original.public_dict(), cold.public_approval_dict()):
            editing = public["editing"]
            assert not PRIVATE_EDIT_VALUES.keys() & editing["schema"]["properties"].keys()
            assert not PRIVATE_EDIT_VALUES.keys() & editing["values"].keys()
            for value in PRIVATE_EDIT_VALUES.values():
                if isinstance(value, str):
                    assert value not in json.dumps(public)
        edited = await rig.pipeline.edit_confirmation(original.plan_id, {"label": "edited"}, context=context)
        assert all(edited.effect_plan.arguments[key] == value for key, value in PRIVATE_EDIT_VALUES.items())
        await rig.pipeline.execute_confirmed(edited.effect_plan.plan_id, context=context)
        assert len(rig.writes) == 1 and rig.value == "edited"
        assert all(rig.writes[0][key] == value for key, value in PRIVATE_EDIT_VALUES.items())
    asyncio.run(run())


@pytest.mark.parametrize("field", PRIVATE_EDIT_VALUES)
def test_sensitive_schema_field_cannot_be_changed_by_edit_patch(rig, field):
    async def run():
        arguments = with_private_fields(rig)
        context = await rig.context()
        plan = (await rig.pipeline.execute(rig.tool.name, arguments, context=context)).effect_plan
        with pytest.raises(ToolPipelineError) as error:
            await rig.pipeline.edit_confirmation(plan.plan_id, {field: PRIVATE_EDIT_VALUES[field]}, context=context)
        assert error.value.code == "confirmation_edit_invalid" and error.value.plan_invalidated
        assert rig.writes == [] and len(rig.prepares) == 1
        with pytest.raises(ConfirmationClaimError):
            await rig.pipeline.execute_confirmed(plan.plan_id, context=context)
    asyncio.run(run())


def test_second_card_created_by_confirmed_continuation_is_editable_under_original_fence(rig):

    from app.agent.kernel.events import AgentEventType as E
    from app.agent.kernel.model import ModelEvent, ModelToolCall
    from app.agent.kernel.model import ModelEventType as M
    from app.agent.kernel.session import AgentSession
    from app.agent.kernel.state_types import AgentInput
    from tests.test_agent_kernel_core import ScriptedModel, collect

    def call(label):
        return [ModelEvent(M.TOOL_CALL_COMPLETED, tool_call=ModelToolCall(label, rig.tool.name,
                    {"label": label, "object_id": "opaque-object-7", "signature": "credential-signed-original"})),
                ModelEvent(M.FINISH, finish_reason="tool_calls")]
    model = ScriptedModel([call("A"), call("B"), [ModelEvent(M.TEXT_DELTA, text="完成"), ModelEvent(M.FINISH, finish_reason="stop")]])
    session = AgentSession(model=model, catalog=rig.pipeline.catalog,
                           pipeline=rig.pipeline, state_store=rig.store)

    async def run():
        initial = await collect(session.run(AgentInput(message="修改名称A，然后修改B。", owner=OWNER, session_id=SESSION)))
        first = next(event.payload["plan"]["plan_id"] for event in initial if event.type == E.EFFECT_APPROVAL_REQUIRED)
        continuation = await collect(session.confirm(owner=OWNER, session_id=SESSION, plan_id=first))
        second = next(event.payload["plan"]["plan_id"] for event in continuation if event.type == E.EFFECT_APPROVAL_REQUIRED)
        assert len(rig.writes) == 1 and rig.value == "A"
        result = await session.edit_confirmation(owner=OWNER, session_id=SESSION, plan_id=second, arguments={"label": "C"})
        assert result.effect_plan is not None
        old = await collect(session.confirm(owner=OWNER, session_id=SESSION, plan_id=second))
        assert old[-1].type == E.TURN_FAILED and len(rig.writes) == 1
        done = await collect(session.confirm(owner=OWNER, session_id=SESSION, plan_id=result.effect_plan.plan_id))
        assert any(event.type == E.EFFECT_COMPLETED for event in done)
        assert [row["label"] for row in rig.writes] == ["A", "C"]
    asyncio.run(run())


def test_edit_rejects_live_confirmation_tail_until_current_execution_finishes(rig):

    from app.agent.kernel.events import AgentEventType as E
    from app.agent.kernel.model import ModelEvent, ModelToolCall
    from app.agent.kernel.model import ModelEventType as M
    from app.agent.kernel.session import AgentSession
    from app.agent.kernel.state_types import AgentInput, SessionBusyError
    from tests.test_agent_kernel_core import ScriptedModel, collect

    def call(label):
        return [ModelEvent(M.TOOL_CALL_COMPLETED, tool_call=ModelToolCall(label, rig.tool.name,
                    {"label": label, "object_id": "opaque-object-7", "signature": "credential-signed-original"})),
                ModelEvent(M.FINISH, finish_reason="tool_calls")]

    session = AgentSession(model=ScriptedModel([call("A"), call("B")]), catalog=rig.pipeline.catalog,
         pipeline=rig.pipeline, state_store=rig.store, journal=rig.store)

    async def run():
        initial = await collect(session.run(AgentInput(message="修改名称A，然后修改B。", owner=OWNER, session_id=SESSION)))
        first = next(event.payload["plan"]["plan_id"] for event in initial if event.type == E.EFFECT_APPROVAL_REQUIRED)
        ready, release = asyncio.Event(), asyncio.Event()
        append = rig.store.append_for_turn

        async def pause_approval(event, lease):
            await append(event, lease)
            if event.type == E.EFFECT_APPROVAL_REQUIRED:
                ready.set()
                await release.wait()

        with patch.object(rig.store, "append_for_turn", pause_approval):
            confirmation = asyncio.create_task(collect(session.confirm(owner=OWNER, session_id=SESSION,
                plan_id=first, request_id="live-confirmation")))
            try:
                await asyncio.wait_for(ready.wait(), 5)
                state = await rig.store.load(owner=OWNER, session_id=SESSION)
                second = state.pending_effect_plan_id
                active = state.metadata["active_publication"]
                current = await session.coordinator.describe(owner=OWNER, session_id=SESSION)
                assert current is not None and active["turn_id"] == current["turn_id"]
                assert active["request_id"] == "live-confirmation" and active["status"] == "running"
                assert second and second != first and not confirmation.done()
                with pytest.raises(SessionBusyError):
                    await session.edit_confirmation(owner=OWNER, session_id=SESSION, plan_id=second, arguments={"label": "C"})
                assert await rig.store.load(owner=OWNER, session_id=SESSION) == state
                pending = await rig.pipeline.get_pending_plan(owner=OWNER, session_id=SESSION)
                assert pending is not None and pending.plan_id == second
                assert len(rig.prepares) == 2 and [row["label"] for row in rig.writes] == ["A"]
            finally:
                release.set()
                await asyncio.wait_for(confirmation, 5)
        edited = await session.edit_confirmation(owner=OWNER, session_id=SESSION, plan_id=second, arguments={"label": "C"})
        assert edited.effect_plan is not None and edited.effect_plan.arguments["label"] == "C"
        assert len(rig.prepares) == 3 and [row["label"] for row in rig.writes] == ["A"]
    asyncio.run(run())
