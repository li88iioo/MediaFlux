"""直接确认写后尾段的活动、取消和停止共用跨 runtime 执行身份。"""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from starlette.requests import Request

from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.events import AgentEventType as E
from app.agent.kernel.state_types import AgentInput, SessionBusyError
from app.agent.task_stop import stop_agent_session
from app.agent.transports import WebKernelTransport
from app.routes import agent_api
from tests.support import agent_llm_enabled as agent_llm_enabled  # noqa: PLC0414
from tests.test_agent_confirmation_edit import OWNER, SESSION
from tests.test_agent_confirmation_liveness import native_call, native_session
from tests.test_agent_confirmation_liveness import rig as rig  # noqa: PLC0414
from tests.test_agent_kernel_core import collect
from tests.test_agent_recovered_progress import reply
from tests.test_agent_tool_argument_ingress import PROTOCOLS, Wire
from tests.test_agent_tool_argument_ingress import offline as offline  # noqa: PLC0414
from tests.test_agent_tool_argument_ingress import store as store  # noqa: PLC0414


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("cold", [False, True], ids=["warm", "cold"])
@pytest.mark.parametrize("followup_read", [False, True], ids=["model-tail", "next-read"])
@pytest.mark.parametrize("operation", ["cancel", "stop"])
def test_direct_confirmation_tail_can_be_observed_and_stopped(agent_llm_enabled, rig, protocol, cold, followup_read, operation):
    async def run():
        ready, release = asyncio.Event(), asyncio.Event()

        class HeldWire(Wire):
            async def handler(self, request):
                if len(self.requests) == 1:
                    ready.set()
                    await release.wait()
                return await super().handler(request)

        reads = []

        def inspect(_arguments, _context):
            reads.append(rig.value)
            return {"ok": True, "summary": "已重新读取名称A", "data": {"value": rig.value}}

        rig.pipeline.catalog.register(KernelToolSpec(name="fixture.inspect", domain="fixture", description="重新读取名称",
            effect=ToolEffect.READ, input_schema={"type": "object", "properties": {}}, read=inspect))
        responses = [native_call(protocol, rig, "A")]
        if followup_read:
            responses.append(reply(protocol, name="fixture__inspect", arguments={}))
        responses.append(reply(protocol, text="名称A已修改，以实际执行回执为准。"))
        wire = HeldWire(protocol, responses)
        source = native_session(rig, wire, journal=rig.store)
        preview = await collect(source.run(AgentInput(owner=OWNER, session_id=SESSION,
            message="把名称修改为A，然后告诉我结果。", request_id="preview-request")))
        execution = asyncio.create_task(collect(source.confirm(owner=OWNER, session_id=SESSION,
            plan_id=preview[-1].payload["plan_id"], request_id="confirmation-request")))
        try:
            await asyncio.wait_for(ready.wait(), 5)
            assert len(rig.writes) == 1
            receiver = native_session(rig, Wire(protocol, []), cold=True) if cold else source
            transport = WebKernelTransport(receiver)
            runtime = SimpleNamespace(store=receiver.state_store, session=receiver, web=transport)
            activity = await transport.activity(owner=OWNER, session_id=SESSION)
            assert activity is not None and activity["request_id"] == "confirmation-request"
            assert activity["status"] == "running" and activity["protected"] is False
            before = await receiver.state_store.load(owner=OWNER, session_id=SESSION)
            assert activity["turn_id"] == before.metadata["confirmed_publication"]["turn_id"]
            assert before.metadata["active_publication"]["request_id"] == "confirmation-request"
            assert not await transport.cancel(owner=OWNER, session_id=SESSION, request_id="old-preview")
            assert await receiver.state_store.load(owner=OWNER, session_id=SESSION) == before
            request = Request({"type": "http", "method": "GET", "path": "/api/agent/sessions/" + SESSION, "headers": []})
            with patch.object(agent_api, "get_agent_kernel_runtime", return_value=runtime), patch.object(
                agent_api, "require_api_login", return_value=None), patch.object(agent_api, "_owner", return_value=OWNER), patch.object(
                agent_api, "_draft_scope", return_value="draft"):
                response = json.loads(bytes((await agent_api.get_session(request, SESSION)).body))
            assert response["active_turn"]["turn_id"] == activity["turn_id"]
            assert response["active_turn"]["protected"] is False
            if operation == "cancel":
                assert await transport.cancel(owner=OWNER, session_id=SESSION, request_id="confirmation-request")
            else:
                stopped = await stop_agent_session(runtime, OWNER, SESSION)
                assert stopped["status"] == "stopping" and stopped["stopped"] is False
                assert stopped["model_turn"] == "stop_requested"
            after = await receiver.state_store.load(owner=OWNER, session_id=SESSION)
            assert after.generation == before.generation + 1
            assert after.metadata["active_publication"]["status"] == "cancelled"
            assert after.metadata["confirmed_publication"] == before.metadata["confirmed_publication"]
            if operation == "stop":
                repeated = await stop_agent_session(runtime, OWNER, SESSION)
                assert repeated["stopped"] is True
                assert await receiver.state_store.load(owner=OWNER, session_id=SESSION) == after
        finally:
            release.set()
        events = await asyncio.wait_for(execution, 5)
        assert events[-1].type is E.TURN_CANCELLED
        assert not await transport.cancel(owner=OWNER, session_id=SESSION, request_id="confirmation-request")
        assert len(rig.writes) == 1 and reads == []
        state = await receiver.state_store.load(owner=OWNER, session_id=SESSION)
        assert state.metadata["active_publication"]["status"] == "cancelled"
        assert not any(row.get("role") == "assistant" and row.get("content") == "名称A已修改，以实际执行回执为准。"
                       for row in state.conversation)

    asyncio.run(run())


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("cold", [False, True], ids=["warm", "cold"])
@pytest.mark.parametrize("operation", ["cancel", "stop"])
def test_direct_confirmation_write_stays_protected_until_receipt(agent_llm_enabled, rig, protocol, cold, operation):
    async def run():
        ready, release = asyncio.Event(), asyncio.Event()
        execute = rig.tool.execute_confirmed
        assert execute is not None

        async def held_execute(*args):
            ready.set()
            await release.wait()
            return execute(*args)

        rig.pipeline.catalog = ToolCatalog([replace(rig.tool, execute_confirmed=held_execute)])
        wire = Wire(protocol, [native_call(protocol, rig, "A"), reply(protocol, text="名称A已修改，以实际执行回执为准。")])
        source = native_session(rig, wire, journal=rig.store)
        preview = await collect(source.run(AgentInput(owner=OWNER, session_id=SESSION,
            message="把名称修改为A，然后告诉我结果。", request_id="preview-request")))
        execution = asyncio.create_task(collect(source.confirm(owner=OWNER, session_id=SESSION,
            plan_id=preview[-1].payload["plan_id"], request_id="confirmation-request")))
        try:
            await asyncio.wait_for(ready.wait(), 5)
            receiver = native_session(rig, Wire(protocol, []), cold=True) if cold else source
            transport = WebKernelTransport(receiver)
            runtime = SimpleNamespace(store=receiver.state_store, session=receiver, web=transport)
            before = await receiver.state_store.load(owner=OWNER, session_id=SESSION)
            activity = await transport.activity(owner=OWNER, session_id=SESSION)
            assert activity is not None and activity["request_id"] == "confirmation-request"
            assert activity["protected"] is True and rig.writes == []
            if operation == "cancel":
                with pytest.raises(SessionBusyError):
                    await transport.cancel(owner=OWNER, session_id=SESSION, request_id="confirmation-request")
                assert await receiver.state_store.load(owner=OWNER, session_id=SESSION) == before
            else:
                stopped = await stop_agent_session(runtime, OWNER, SESSION)
                assert stopped["status"] == "critical_pending" and stopped["stopped"] is False
                assert stopped["model_turn"] == "critical_pending"
                assert stopped["confirmation"] == "unchanged"
                after = await receiver.state_store.load(owner=OWNER, session_id=SESSION)
                assert after.generation == before.generation
                assert after.metadata["confirmed_publication"] == before.metadata["confirmed_publication"]
                assert after.metadata["stop_requested_generation"] == before.generation
            assert not execution.done() and rig.writes == []
        finally:
            release.set()
        events = await asyncio.wait_for(execution, 5)
        assert events[-1].type is (E.TURN_CANCELLED if operation == "stop" else E.TURN_COMPLETED)
        assert len(rig.writes) == 1
        assert await transport.activity(owner=OWNER, session_id=SESSION) is None

    asyncio.run(run())
