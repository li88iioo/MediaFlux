"""渠道开关只撤销该渠道读取；原持久确认 fence 仍按配置变化失效。"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from app import config, sdk
from app.agent.errors import ToolPipelineError
from app.agent.feature_gate import current_agent_runtime_generation
from app.agent.kernel.bootstrap import build_agent_kernel_runtime
from app.agent.kernel.model import ModelEvent, ModelEventType
from app.agent.kernel.state_types import AgentInput
from app.agent.owner_routes import web_kernel_owner
from app.bot.agent_adapter import _apply_agent_control_action
from tests import test_agent_generation_profiles as profiles
from tests.test_agent_sdk_effects import context_for

local_config = profiles.local_config
config_api = profiles.config_api
no_external_io = profiles.no_external_io

WEB_OWNER = web_kernel_owner("channel-audit")
TG_OWNER = "tg:v1:-100\x1f7"


class HeldModel:
    def __init__(self):
        self.entered = set()
        self.ready = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(self, request, *, cancellation):
        self.entered.add(request.messages[-1].content)
        if len(self.entered) == 2:
            self.ready.set()
        await self.release.wait()
        yield ModelEvent(ModelEventType.TEXT_DELTA, text="已取得本次查询结果")
        yield ModelEvent(ModelEventType.FINISH, finish_reason="stop")


def configure_telegram():
    config.set_and_save({"TG_AGENT_ENABLED": "1", "TG_CHAT_ID": "-100", "TG_AGENT_ALLOWED_USER_IDS": "7",
                         "TG_BOT_TOKEN": "123456:offline_test_token"})


def toggle(entry, api, *, reopen=False, global_off=False):
    if entry == "web":
        updates = {"AGENT_LLM_ENABLED": "0"} if global_off else {"TG_AGENT_ENABLED": "0"}
        response = api.post("/api/config", json=updates)
        assert response.status_code == 200, response.text
        if reopen:
            response = api.post("/api/config", json={"TG_AGENT_ENABLED": "1"})
            assert response.status_code == 200, response.text
    else:
        with patch("app.modules.agent_runtime.request_agent_runtime_reconcile", return_value=True), \
             patch("app.bot.handlers.request_command_menu_refresh", return_value=True):
            _apply_agent_control_action("disable_all" if global_off else "disable_telegram")
            if reopen:
                _apply_agent_control_action("enable_telegram")


@pytest.mark.parametrize("entry", ["web", "telegram"])
@pytest.mark.parametrize("reopen", [False, True])
def test_telegram_toggle_preserves_inflight_web_but_revokes_old_telegram(config_api, entry, reopen):
    configure_telegram()

    async def run():
        model = HeldModel()
        runtime = build_agent_kernel_runtime(model=model)
        async def query(owner, channel):
            return [event async for event in runtime.session.run(AgentInput(
                owner=owner, session_id="channel_lifecycle_" + channel,
                message="查询" + channel, request_id="channel_request_" + channel, channel=channel))]
        web = asyncio.create_task(query(WEB_OWNER, "web"))
        telegram = asyncio.create_task(query(TG_OWNER, "telegram"))
        try:
            await asyncio.wait_for(model.ready.wait(), timeout=5)
            global_generation = current_agent_runtime_generation()
            toggle(entry, config_api, reopen=reopen)
            assert current_agent_runtime_generation() == global_generation
        finally:
            model.release.set()
        web_events, telegram_events = await asyncio.wait_for(asyncio.gather(web, telegram), timeout=5)
        assert web_events[-1].type.value == "turn.completed"
        assert web_events[-1].payload["status"] == "success"
        assert web_events[-1].payload["answer"] == "已取得本次查询结果"
        assert telegram_events[-1].type.value == "turn.cancelled"
        assert not any(event.type.value == "turn.completed" for event in telegram_events)

    asyncio.run(run())


@pytest.mark.parametrize("entry", ["web", "telegram"])
def test_global_disable_still_revokes_web_and_telegram(config_api, entry):
    configure_telegram()

    async def run():
        model = HeldModel()
        runtime = build_agent_kernel_runtime(model=model)
        async def query(owner, channel):
            return [event async for event in runtime.session.run(AgentInput(
                owner=owner, session_id="global_lifecycle_" + channel,
                message="查询" + channel, channel=channel))]
        jobs = [asyncio.create_task(query(WEB_OWNER, "web")), asyncio.create_task(query(TG_OWNER, "telegram"))]
        try:
            await asyncio.wait_for(model.ready.wait(), timeout=5)
            toggle(entry, config_api, global_off=True)
        finally:
            model.release.set()
        for events in await asyncio.wait_for(asyncio.gather(*jobs), timeout=5):
            assert events[-1].type.value == "turn.cancelled"

    asyncio.run(run())


@pytest.mark.parametrize("entry", ["web", "telegram"])
def test_channel_toggle_keeps_durable_confirmation_revocation(config_api, entry):
    configure_telegram()

    async def run():
        runtime = build_agent_kernel_runtime(model=HeldModel())
        prepared = []
        for owner in (WEB_OWNER, TG_OWNER):
            context = await context_for(runtime.store, owner=owner)
            preview = await runtime.session.pipeline.prepare("preferences.update", {"preferred_resolution": "2160p"}, context=context)
            assert preview.effect_plan is not None
            prepared.append((context, preview.effect_plan))
        toggle(entry, config_api, reopen=True)
        for context, plan in prepared:
            with pytest.raises(ToolPipelineError) as rejected:
                await runtime.session.pipeline.execute_confirmed(plan.plan_id, context=context)
            assert rejected.value.code == "confirmation_stale"
            assert rejected.value.execution_started is False
            with sdk.call_owner(context.owner):
                assert sdk.preferences.get()["preferred_resolution"] == "any"

    asyncio.run(run())
