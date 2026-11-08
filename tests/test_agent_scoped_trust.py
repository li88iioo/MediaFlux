"""真实本地收藏库、native port、SQLite 票据和原 Session 的限定预授权。"""

from typing import Any
import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app import database as db
from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.errors import ConfirmationClaimError, ToolPipelineError
from app.agent.kernel.capabilities import ToolCatalog, ToolEffect
from app.agent.kernel.conversation_codec import conversation_cost
from app.agent.kernel.effects import ConfirmationEffectPlanStore
from app.agent.kernel.events import AgentEventType as Event
from app.agent.kernel.model import ModelEvent, ModelEventType, ModelToolCall
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.ports.sdk_effects import watchlist_tools
from app.agent.kernel.session import AgentSession
from app.agent.kernel.state_types import (
    AgentInput,
    CancellationToken,
    SessionBusyError,
    StateUpdate,
)
from app.agent.kernel.tool_calls import ToolCallContext
from app.discovery.models import MediaCard
from tests.test_agent_kernel_core import ScriptedModel, collect
from tests.test_agent_task_cas import task_store as isolated_store

task_store = isolated_store
OWNER, SESSION, TOOL = "scoped-trust-owner", "scoped-trust-session", "watchlist.add"
SCOPE = {"provider": "tmdb", "media_type": "tv"}
FINAL = [ModelEvent(ModelEventType.TEXT_DELTA, text="以收藏回执为准。"), ModelEvent(ModelEventType.FINISH, finish_reason="stop")]


def arguments(number=100, *, provider="tmdb", media_type="tv"):
    return {"provider": provider, "media_type": media_type, "external_id": str(number)}


def call(args):
    return [ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall("favorite-call", TOOL, args)),
            ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")]


class TrustRig:
    def __init__(self, store):
        self.now = 2_000_000_000.0
        self.store, self.attempts = store, []
        self.fail = False
        tool = next(spec for spec in watchlist_tools() if spec.name == TOOL)
        original = tool.execute_confirmed
        def execute(args, snapshot, context):
            self.attempts.append(dict(args))
            if self.fail:
                raise ToolPipelineError("注入执行失败", code="fixture_failure")
            assert original is not None
            return original(args, snapshot, context)
        self.tool = replace(tool, execute_confirmed=execute)
        self.pipeline = ToolPipeline(catalog=ToolCatalog([self.tool]), state_store=store,
            effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore(clock=lambda: self.now)))

    async def context(self, *, owner=OWNER, session_id=SESSION):
        lease, _ = await self.store.begin_turn(owner=owner, session_id=session_id, request_id="trust-test")
        async def progress(_payload):
            pass
        return ToolCallContext(owner, session_id, lease.request_id, lease.turn_id, lease, CancellationToken(), progress)

    async def preview(self, context, args=None):
        return (await self.pipeline.execute(TOOL, args or arguments(), context=context)).effect_plan

    async def grant(self, context, plan, **overrides):
        selection: dict[str, Any] = dict(operation="grant", plan_id=plan.plan_id, tool_name=TOOL, scope=SCOPE, max_actions=5, ttl_seconds=300)
        return await self.pipeline.scoped_trust.set(context=context, **{**selection, **overrides})

    async def remaining(self):
        return (await self.store.load(owner=OWNER, session_id=SESSION)).metadata["scoped_trust"]["remaining"]

    def session(self, rounds):
        return AgentSession(model=ScriptedModel(rounds), catalog=self.pipeline.catalog,
             pipeline=self.pipeline, state_store=self.store)


@pytest.fixture
def rig(task_store, monkeypatch):
    monkeypatch.setattr("app.modules.web_secret.get_web_secret", lambda: "scoped-trust-test-only")
    monkeypatch.setattr("app.agent.kernel.ports.sdk_effects.config.get_bool", lambda *_args, **_kwargs: True)
    monkeypatch.setattr("app.discovery.service.DiscoveryService.get_detail",
        lambda self, provider, media_type, external_id: MediaCard(
            provider=provider, external_id=external_id, media_type=media_type,
            title=f"测试收藏 {external_id}", year="2026"))
    return TrustRig(task_store)


def test_default_off_explicit_scope_allows_different_items_and_debits_once(rig):
    async def run():
        context = await rig.context()
        first = await rig.preview(context)
        assert first.public_dict()["trust_scope"] == SCOPE
        assert not await rig.pipeline.scoped_trust.allows(first, context=context)
        with pytest.raises(ConfirmationClaimError):
            await rig.pipeline.execute_confirmed(first.plan_id, context=context, scoped_trust=True)
        grant = await rig.grant(context, first)
        assert grant["active"] and grant["remaining"] == 5 and grant["scope"] == SCOPE
        assert rig.attempts == [] and db.get_media_watchlist("tmdb", "100", "tv") is None
        for number in range(101, 106):
            plan = await rig.preview(context, arguments(number))
            assert await rig.pipeline.scoped_trust.allows(plan, context=context)
            result = await rig.pipeline.execute_confirmed(plan.plan_id, context=context, scoped_trust=True)
            assert result.outcome.public_content["ok"] is True
            assert db.get_media_watchlist("tmdb", str(number), "tv") is not None
            assert await rig.remaining() == 105 - number
            with pytest.raises(ConfirmationClaimError):
                await rig.pipeline.execute_confirmed(plan.plan_id, context=context, scoped_trust=True)
            assert await rig.remaining() == 105 - number
        sixth = await rig.preview(context, arguments(106))
        assert not await rig.pipeline.scoped_trust.allows(sixth, context=context)
        with pytest.raises(ConfirmationClaimError):
            await rig.pipeline.execute_confirmed(sixth.plan_id, context=context, scoped_trust=True)
        assert len(rig.attempts) == 5 and db.get_media_watchlist("tmdb", "106", "tv") is None
        assert (await rig.pipeline.get_pending_plan(owner=OWNER, session_id=SESSION)).plan_id == sixth.plan_id
    asyncio.run(run())


@pytest.mark.parametrize("tool_field", [{}, {"tool_name": None}])
def test_grant_without_tool_name_has_inactive_view(rig, tool_field):
    async def run():
        state = await rig.store.load(owner=OWNER, session_id=SESSION)
        state.metadata["scoped_trust"] = {
            "owner": OWNER, "session_id": SESSION, "scope": SCOPE,
            "remaining": 5, "expires_at": rig.now + 300, **tool_field,
        }
        assert rig.pipeline.scoped_trust._grant(state) is not None
        assert rig.pipeline.scoped_trust.view(state) == {"active": False}
    asyncio.run(run())


@pytest.mark.parametrize("overrides", [{"ttl_seconds": 0}, {"ttl_seconds": True}, {"ttl_seconds": 3600},
    {"max_actions": 0}, {"max_actions": True}, {"max_actions": 100}, {"tool_name": "cloud.share.create"},
    {"scope": {"provider": "tmdb", "media_type": "movie"}}, {"scope": {"provider": "tmdb"}}])
def test_grant_requires_explicit_tool_exact_scope_count_and_finite_ttl(rig, overrides):
    async def run():
        context = await rig.context()
        first = await rig.preview(context)
        with pytest.raises(ToolPipelineError):
            await rig.grant(context, first, **overrides)
        assert rig.attempts == []
        assert not await rig.pipeline.scoped_trust.allows(first, context=context)
    asyncio.run(run())


@pytest.mark.parametrize("args", [arguments(201, media_type="movie"), arguments(201, provider="douban")])
def test_scope_change_requires_new_confirmation_and_does_not_spend(rig, args):
    async def run():
        context = await rig.context()
        await rig.grant(context, await rig.preview(context))
        plan = await rig.preview(context, args)
        assert not await rig.pipeline.scoped_trust.allows(plan, context=context)
        with pytest.raises(ConfirmationClaimError):
            await rig.pipeline.execute_confirmed(plan.plan_id, context=context, scoped_trust=True)
        assert await rig.remaining() == 5 and rig.attempts == []
        assert (await rig.pipeline.get_pending_plan(owner=OWNER, session_id=SESSION)).plan_id == plan.plan_id
    asyncio.run(run())


@pytest.mark.parametrize("name,risk,effect", [("cloud.share.create", "low_write", ToolEffect.WRITE),
    (TOOL, "write", ToolEffect.WRITE), (TOOL, "low_write", ToolEffect.DANGER)])
def test_low_write_label_alone_write_and_danger_cannot_bypass(rig, name, risk, effect):
    async def run():
        context = await rig.context()
        await rig.grant(context, await rig.preview(context))
        changed = replace(rig.tool, name=name, model_name=name.replace(".", "__"), effect=effect,
                          metadata={**rig.tool.metadata, "risk": risk})
        rig.pipeline = ToolPipeline(catalog=ToolCatalog([changed]), state_store=rig.store,
            effect_store=rig.pipeline.effect_store)
        plan = (await rig.pipeline.execute(name, arguments(301), context=context)).effect_plan
        assert plan is not None
        assert not plan.public_dict().get("trust_eligible")
        with pytest.raises(ConfirmationClaimError):
            await rig.pipeline.execute_confirmed(plan.plan_id, context=context, scoped_trust=True)
        assert await rig.remaining() == 5 and rig.attempts == []
    asyncio.run(run())


@pytest.mark.parametrize("owner,sid", [("foreign-owner", SESSION), (OWNER, "foreign-session")])
def test_other_session_and_owner_cannot_use_or_revoke_grant_or_ticket(rig, owner, sid):
    async def run():
        context = await rig.context()
        plan = await rig.preview(context)
        await rig.grant(context, plan)
        foreign = await rig.context(owner=owner, session_id=sid)
        with pytest.raises(ConfirmationClaimError):
            await rig.pipeline.execute_confirmed(plan.plan_id, context=foreign, scoped_trust=True)
        await rig.pipeline.scoped_trust.set(context=foreign, operation="revoke")
        assert await rig.remaining() == 5
        assert await rig.pipeline.scoped_trust.allows(plan, context=context)
        assert rig.attempts == []
    asyncio.run(run())


def test_expiry_and_revoke_restore_manual_confirmation(rig):
    async def run():
        context = await rig.context()
        plan = await rig.preview(context)
        await rig.grant(context, plan)
        rig.now += 300
        assert not await rig.pipeline.scoped_trust.allows(plan, context=context)
        with pytest.raises(ConfirmationClaimError):
            await rig.pipeline.execute_confirmed(plan.plan_id, context=context, scoped_trust=True)
        await rig.grant(context, plan)
        await rig.pipeline.scoped_trust.set(context=context, operation="revoke")
        with pytest.raises(ConfirmationClaimError):
            await rig.pipeline.execute_confirmed(plan.plan_id, context=context, scoped_trust=True)
        assert rig.attempts == []
        await rig.pipeline.execute_confirmed(plan.plan_id, context=context)
        assert db.get_media_watchlist("tmdb", "100", "tv") is not None
    asyncio.run(run())


def test_handler_failure_spends_once_and_duplicate_never_spends_or_writes_again(rig):
    async def run():
        context = await rig.context()
        plan = await rig.preview(context)
        await rig.grant(context, plan)
        rig.fail = True
        with pytest.raises(ToolPipelineError) as failure:
            await rig.pipeline.execute_confirmed(plan.plan_id, context=context, scoped_trust=True)
        assert failure.value.execution_started is True
        assert await rig.remaining() == 4 and len(rig.attempts) == 1
        with pytest.raises(ConfirmationClaimError):
            await rig.pipeline.execute_confirmed(plan.plan_id, context=context, scoped_trust=True)
        assert await rig.remaining() == 4 and len(rig.attempts) == 1
        assert db.get_media_watchlist("tmdb", "100", "tv") is None
    asyncio.run(run())


def test_expiry_during_confirmation_admission_does_not_spend_or_start_handler(rig):
    async def run():
        context = await rig.context()
        plan = await rig.preview(context)
        await rig.grant(context, plan)
        async def acquire(**_kwargs):
            rig.now += 300
        rig.pipeline.rate_limiter = SimpleNamespace(acquire=acquire)
        with pytest.raises(ToolPipelineError) as failure:
            await rig.pipeline.execute_confirmed(plan.plan_id, context=context, scoped_trust=True)
        assert failure.value.code == "scoped_trust_expired" and failure.value.execution_started is False
        assert await rig.remaining() == 5 and rig.attempts == []
    asyncio.run(run())


def test_execute_and_revoke_are_ordered_by_existing_session_guard(rig):
    async def run():
        context = await rig.context()
        plan = await rig.preview(context)
        await rig.grant(context, plan)
        entered, release = asyncio.Event(), asyncio.Event()
        original = rig.tool.execute_confirmed
        async def execute(*args):
            entered.set()
            await release.wait()
            return original(*args)
        rig.pipeline = ToolPipeline(catalog=ToolCatalog([replace(rig.tool, execute_confirmed=execute)]),
            state_store=rig.store, effect_store=rig.pipeline.effect_store)
        executing = asyncio.create_task(rig.pipeline.execute_confirmed(plan.plan_id, context=context, scoped_trust=True))
        await asyncio.wait_for(entered.wait(), 5)
        with pytest.raises(SessionBusyError):
            await rig.pipeline.scoped_trust.set(context=context, operation="revoke")
        release.set()
        await executing
        assert len(rig.attempts) == 1 and await rig.remaining() == 4
        assert await rig.pipeline.scoped_trust.set(context=context, operation="revoke") == {"active": False}
        next_plan = await rig.preview(context, arguments(402))
        assert not await rig.pipeline.scoped_trust.allows(next_plan, context=context)
    asyncio.run(run())


def test_tool_json_and_effect_callbacks_cannot_forge_grants(rig):
    async def run():
        context = await rig.context()
        for key in ("metadata.scoped_trust", "metadata.scoped_trust.remaining"):
            with pytest.raises(ValueError):
                await rig.store.commit(context.lease, updates=(StateUpdate(key, {"remaining": 999}),))
        with pytest.raises(ValueError):
            await rig.store.update_effect_state(owner=OWNER, session_id=SESSION,
                change=lambda state: state.metadata.update(scoped_trust={"remaining": 999}))
    asyncio.run(run())


def test_full_session_different_tv_automatic_movie_and_revoked_scope_pause(rig):
    async def run():
        session = rig.session([call(arguments()), call(arguments(501)), FINAL,
                               call(arguments(502, media_type="movie")), call(arguments(503))])
        initial = await collect(session.run(AgentInput(owner=OWNER, session_id=SESSION, message="把这部剧加入探索收藏")))
        plan = next(event.payload["plan"] for event in initial if event.type is Event.EFFECT_APPROVAL_REQUIRED)
        grant = await session.set_scoped_trust(owner=OWNER, session_id=SESSION, operation="grant", plan_id=plan["plan_id"],
            tool_name=TOOL, scope=SCOPE, max_actions=5, ttl_seconds=300)
        assert grant["active"] and rig.attempts == []
        auto = await collect(session.run(AgentInput(owner=OWNER, session_id=SESSION, message="把另一部电视剧也加入探索收藏")))
        assert not any(event.type is Event.EFFECT_APPROVAL_REQUIRED for event in auto)
        assert any(event.type is Event.EFFECT_COMPLETED and event.payload["scoped_trust"]["remaining"] == 4 for event in auto)
        assert db.get_media_watchlist("tmdb", "501", "tv") is not None
        assert db.get_media_watchlist("tmdb", "100", "tv") is None
        assert await rig.remaining() == 4 and len(rig.attempts) == 1
        movie = await collect(session.run(AgentInput(owner=OWNER, session_id=SESSION, message="把这部电影也加入探索收藏")))
        assert any(event.type is Event.EFFECT_APPROVAL_REQUIRED for event in movie)
        assert db.get_media_watchlist("tmdb", "502", "movie") is None
        await session.set_scoped_trust(owner=OWNER, session_id=SESSION, operation="revoke")
        revoked = await collect(session.run(AgentInput(owner=OWNER, session_id=SESSION, message="把下一部剧加入探索收藏")))
        assert any(event.type is Event.EFFECT_APPROVAL_REQUIRED for event in revoked)
        assert db.get_media_watchlist("tmdb", "503", "tv") is None
        state = await rig.store.load(owner=OWNER, session_id=SESSION)
        assert "scope_digest" not in json.dumps(rig.pipeline.scoped_trust.view(state))
    asyncio.run(run())


@pytest.mark.parametrize("near_capacity", [False, True], ids=["short-history", "retained-history"])
@pytest.mark.parametrize("effect_count", [1, 2], ids=["answer", "next-effect"])
def test_automatic_confirmation_retention_keeps_user_and_receipt_identity(rig, near_capacity, effect_count):
    async def run():
        context = await rig.context()
        await rig.grant(context, await rig.preview(context))
        request = "把另一部电视剧也加入探索收藏"
        rows: list[dict[str, Any]] = [row for index in range(17) for row in (
            {"role": "user", "content": f"旧请求{index}:" + "字" * 3100,
             "history_id": f"old-{index}", "history_order": index},
            {"role": "assistant", "content": "已记录"},
        )] if near_capacity else []
        rows.extend([
            {"role": "user", "content": request, "history_id": "same-text-earlier"},
            {"role": "assistant", "content": "先前问题已结束"},
        ])
        if near_capacity:
            rows[-1]["content"] += "a" * (163700 - conversation_cost(rows))
        seeded = await rig.store.commit(context.lease, conversation=rows)
        session = rig.session([*(call(arguments(501 + index)) for index in range(effect_count)), FINAL])
        events = await collect(session.run(AgentInput(owner=OWNER, session_id=SESSION, message=request)))
        assert events[-1].type is Event.TURN_COMPLETED and events[-1].payload["status"] == "success"
        assert [item["external_id"] for item in rig.attempts] == [str(501 + index) for index in range(effect_count)]
        cold = await rig.store.load(owner=OWNER, session_id=SESSION)
        if near_capacity:
            assert any(row["history_id"] not in {item.get("history_id") for item in cold.conversation}
                       for row in seeded.conversation if row.get("history_id"))
        users = [row for row in cold.conversation if row.get("role") == "user" and row["content"] == request]
        assert len(users) == 2 and len({row["history_id"] for row in users}) == 2
        effects = [event.payload for event in events if event.type is Event.EFFECT_COMPLETED]
        for effect in effects:
            tool_rows = [row for row in cold.conversation
                         if row.get("role") == "tool" and row.get("effect_plan_id") == effect["plan_id"]]
            assert len(tool_rows) == 1
            result = json.loads(tool_rows[0]["content"])
            assert result["status"] == effect["result"]["status"]
            assert result["summary"] == effect["result"]["summary"]
            assert len([row for row in cold.conversation
                        if row.get("completion_receipt_id") == effect["plan_id"]]) == 1
        for model_request in session.model.requests[1:]:
            pending = set()
            for message in model_request.messages:
                if message.tool_calls:
                    assert not pending
                    pending.update(call.call_id for call in message.tool_calls)
                elif message.role == "tool":
                    assert message.tool_call_id in pending
                    pending.remove(message.tool_call_id)
            assert not pending
    asyncio.run(run())
