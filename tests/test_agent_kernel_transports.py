from __future__ import annotations
from typing import cast
from app.agent.kernel.model import ModelAdapter
from app.agent.kernel.session import TurnAdmissionPolicy

import asyncio
import json
import sqlite3
import unittest
from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import patch

from starlette.requests import Request

from app.agent.kernel.capabilities import (
    CapabilityRetriever,
    KernelToolSpec,
    ToolCatalog,
    ToolEffect,
)
from app.agent.kernel.events import AgentEvent, AgentEventType
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.model import (
    ModelEvent,
    ModelEventType,
    ModelRequest,
    ModelToolCall,
)
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.session import AgentSession
from app.agent.kernel.state import InMemorySessionStateStore
from app.agent.kernel.state_types import AgentInput, _PublicationFinished
from app.agent.transports import (
    EffectEnvelope,
    QueryEnvelope,
    TelegramKernelTransport,
    TransportInputError,
    WebKernelTransport,
)
from app.routes import agent_api
from tests.support import isolated_test_database


class ReadThenAnswerModel:
    async def stream(
        self, request: ModelRequest, *, cancellation
    ) -> AsyncIterator[ModelEvent]:
        cancellation.raise_if_cancelled()
        has_result = any(message.role == "tool" for message in request.messages)
        if not has_result:
            yield ModelEvent(
                ModelEventType.TOOL_CALL_COMPLETED,
                tool_call=ModelToolCall("call-1", "library__count", {}),
            )
            yield ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")
        else:
            yield ModelEvent(ModelEventType.TEXT_DELTA, text="媒体库共有 37 集")
            yield ModelEvent(ModelEventType.FINISH, finish_reason="stop")


def make_session() -> AgentSession:
    tool = KernelToolSpec(
        name="library.count",
        domain="library",
        description="读取媒体库剧集数量",
        examples=("媒体库有多少集",),
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        effect=ToolEffect.READ,
        read=lambda _arguments, _context: {
            "summary": "读取完成",
            "data": {"count": 37},
        },
    )
    catalog = ToolCatalog([tool])
    state = InMemorySessionStateStore()
    return AgentSession(
        model=ReadThenAnswerModel(),
        catalog=catalog,
        retriever=CapabilityRetriever(minimum=1, maximum=1),
        pipeline=ToolPipeline(catalog=catalog, state_store=state),
        state_store=state,
    )


class AgentKernelTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_finished_turn_clears_activity_even_if_final_publication_write_fails(self):
        with isolated_test_database():
            template = make_session()
            store = SQLiteKernelStore(secret_provider=lambda: "isolated-finish-secret")
            session = AgentSession(
                model=template.model, catalog=template.catalog, retriever=template.retriever,
                pipeline=ToolPipeline(catalog=template.catalog, state_store=store),
                state_store=store, journal=store,
            )
            commit = store.commit

            async def fail_finish(lease, **kwargs):
                if any(isinstance(update, _PublicationFinished) for update in kwargs.get('updates', ())):
                    raise sqlite3.OperationalError('synthetic finish write failure')
                return await commit(lease, **kwargs)

            events = []
            with patch.object(store, 'commit', side_effect=fail_finish), self.assertRaises(sqlite3.OperationalError):
                async for event in session.run(AgentInput(
                    owner='owner', session_id='finish-session-0001', request_id='request-1',
                    message='媒体库有多少集', channel='web',
                )):
                    events.append(event)
            self.assertEqual(events[-1].type, AgentEventType.TURN_COMPLETED)
            self.assertIsNone(await session.coordinator.describe(owner='owner', session_id='finish-session-0001'))
            self.assertIsNone(await session.activity(owner='owner', session_id='finish-session-0001'))
            request = Request({'type': 'http', 'method': 'GET', 'path': '/', 'headers': []})
            runtime = SimpleNamespace(session=session, store=store, web=WebKernelTransport(session))
            with patch.object(agent_api, 'require_api_login'), \
                    patch.object(agent_api, '_owner', return_value='owner'), \
                    patch.object(agent_api, '_draft_scope', return_value='fixture'), \
                    patch.object(agent_api, 'get_agent_kernel_runtime', return_value=runtime):
                response = await agent_api.get_session(request, 'finish-session-0001')
            self.assertEqual(response.status_code, 200)
            snapshot = json.loads(bytes(response.body))
            self.assertIsNone(snapshot['active_turn'])
            self.assertEqual(snapshot['last_turn']['status'], 'completed')
            following = [event async for event in session.run(AgentInput(
                owner='owner', session_id='finish-session-0001', request_id='request-2',
                message='再核查一次', channel='web',
            ))]
            self.assertEqual(following[-1].type, AgentEventType.TURN_COMPLETED)
            self.assertIsNone(await session.activity(owner='owner', session_id='finish-session-0001'))

    async def test_cancel_effect_passes_only_normalized_cancellation_arguments(self):
        for transport_type in (WebKernelTransport, TelegramKernelTransport):
            with self.subTest(transport=transport_type.__name__):
                session = make_session()
                with patch.object(session, "cancel_effect", autospec=True, return_value=True) as cancel:
                    self.assertTrue(await transport_type(session).cancel_effect(EffectEnvelope(
                        owner=" owner-1 ", session_id=" session-1 ",
                        plan_id=" plan-cancel-test-0001 ", request_id=" request-1 ", channel="telegram",
                    )))
                cancel.assert_awaited_once_with(
                    owner="owner-1", session_id="session-1", plan_id="plan-cancel-test-0001", request_id="request-1",
                )

    @patch("app.modules.telegram_model_preferences.get_telegram_model_preference", return_value="")
    async def test_web_and_telegram_use_the_same_kernel_event_contract(self, _preference) -> None:
        web = WebKernelTransport(make_session())
        web_request = QueryEnvelope(
            owner="owner-1",
            session_id="web-session",
            message="媒体库有多少集",
            request_id="request-web",
            channel="web",
        )
        web_events = [json.loads(chunk) async for chunk in web.query(web_request)]

        telegram_events: list[AgentEvent] = []

        async def observe(event: AgentEvent) -> None:
            telegram_events.append(event)

        telegram = TelegramKernelTransport(make_session())
        view = await telegram.query(
            QueryEnvelope(
                owner="owner-1",
                session_id="telegram-session",
                message="媒体库有多少集",
                request_id="request-telegram",
            ),
            observe=observe,
        )

        self.assertEqual(
            [event["type"] for event in web_events],
            [event.type.value for event in telegram_events],
        )
        self.assertEqual(view.status, "success")
        self.assertEqual(view.answer, "媒体库共有 37 集")
        self.assertEqual(web_events[-1]["payload"]["answer"], view.answer)

    async def test_transport_accepts_internal_telegram_owner_scope(self) -> None:
        request = QueryEnvelope(
            owner="tg:v1:-123\x1f456",
            session_id="tg-session",
            message="hello",
            channel="telegram",
        )
        self.assertEqual(request.to_agent_input().owner, "tg:v1:-123\x1f456")

    async def test_transport_rejects_invalid_scope_before_kernel(self) -> None:
        request = QueryEnvelope(
            owner="owner", session_id="bad session", message="hello"
        )
        with self.assertRaises(TransportInputError):
            request.to_agent_input()

class TelegramCancellationTests(unittest.IsolatedAsyncioTestCase):
    @patch("app.modules.telegram_model_preferences.get_telegram_model_preference", return_value="")
    async def test_stop_requested_before_turn_start_is_not_lost(self, _preference):
        import asyncio

        from app.agent.kernel.events import AgentEventType
        from app.agent.kernel.state_types import CancellationToken

        session = make_session()
        original = session.run
        async def delayed_start(value):
            await asyncio.sleep(0.025)
            async for event in original(value):
                yield event
        self.enterContext(patch.object(session, "run", delayed_start))
        class SlowModel:
            async def stream(self, request, *, cancellation):
                await cancellation.wait()
                cancellation.raise_if_cancelled()
                yield
        session.model = cast(ModelAdapter, SlowModel())
        token = CancellationToken()
        observed = []
        async def observe(event):
            observed.append(event.type)
        task = asyncio.create_task(TelegramKernelTransport(session).query(
            QueryEnvelope(owner="owner", session_id="stop-before-start", message="slow"),
            cancellation=token, observe=observe,
        ))
        token.cancel("service_stopping")
        view = await asyncio.wait_for(task, 1)
        self.assertEqual(view.status, "cancelled")
        self.assertIn(AgentEventType.TURN_STARTED, observed)
        self.assertIn(AgentEventType.TURN_CANCELLED, observed)


class WebDetachedTurnTests(unittest.IsolatedAsyncioTestCase):
    async def wait_idle(self, session):
        async def wait():
            while await session.coordinator.describe(owner="owner", session_id="session") or session._detached_tasks:
                await asyncio.sleep(0.01)
        await asyncio.wait_for(wait(), 2)

    def paused_session(self):
        entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = []
        class PausedModel:
            async def stream(self, request, *, cancellation):
                calls.append(request)
                entered.set()
                try:
                    await release.wait()
                    yield ModelEvent(ModelEventType.TEXT_DELTA, text="离开页面后仍完成")
                    yield ModelEvent(ModelEventType.FINISH, finish_reason="stop")
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
        session = make_session()
        session.model = PausedModel()
        return session, entered, release, cancelled, calls

    async def test_web_disconnect_completes_once_and_can_be_observed_without_resubmitting(self):
        session, entered, release, cancelled, calls = self.paused_session()
        web = WebKernelTransport(session)
        stream = web.query(QueryEnvelope(owner="owner", session_id="session", request_id="request", message="测试后台继续"))
        self.assertEqual(json.loads(await anext(stream))["type"], "turn.started")
        await asyncio.wait_for(entered.wait(), 1)
        await stream.aclose()
        for _ in range(3):
            activity = await web.activity(owner="owner", session_id="session")
            assert activity is not None
            self.assertEqual(activity["request_id"], "request")
            self.assertEqual(activity["status"], "running")
            self.assertFalse(activity["protected"])
        self.assertIsNone(await web.activity(owner="other", session_id="session"))
        self.assertFalse(cancelled.is_set())
        self.assertTrue(session._detached_tasks)
        self.assertEqual(session.metrics.snapshot()["turns"], 0)
        release.set()
        await self.wait_idle(session)
        state = await session.state_store.load(owner="owner", session_id="session")
        self.assertEqual(state.conversation[-1]["content"], "离开页面后仍完成")
        self.assertEqual(len(calls), 1)
        self.assertEqual(sum(row["role"] == "user" for row in state.conversation), 1)
        metrics = session.metrics.snapshot()
        self.assertEqual(metrics["statuses"], {"success": 1})
        self.assertEqual(metrics["recent"][0]["model_calls"], 1)
        self.assertEqual(metrics["recent"][0]["request_id"], "request")

    async def test_explicit_stop_interrupts_wait_for_first_token_and_matches_request(self):
        session, entered, release, cancelled, calls = self.paused_session()
        web = WebKernelTransport(session)
        stream = web.query(QueryEnvelope(owner="owner", session_id="session", request_id="current", message="等待回复"))
        await anext(stream)
        await asyncio.wait_for(entered.wait(), 1)
        await stream.aclose()
        self.assertFalse(await web.cancel(owner="owner", session_id="session", request_id="old"))
        self.assertFalse(await web.cancel(owner="other", session_id="session", request_id="current"))
        self.assertTrue(await web.cancel(owner="owner", session_id="session", request_id="current"))
        self.assertTrue(await web.cancel(owner="owner", session_id="session", request_id="current"))
        await asyncio.wait_for(cancelled.wait(), 1)
        await self.wait_idle(session)
        self.assertFalse(release.is_set())
        self.assertEqual(len(calls), 1)
        self.assertFalse(await web.cancel(owner="owner", session_id="session", request_id="current"))
        self.assertEqual(session.metrics.snapshot()["statuses"], {"cancelled": 1})

    async def test_non_web_consumer_close_keeps_original_cancellation_semantics(self):
        from app.agent.kernel.state_types import AgentInput
        session, entered, release, cancelled, calls = self.paused_session()
        stream = session.run(AgentInput(owner="owner", session_id="session", message="原API取消", channel="api"))
        await anext(stream)
        await asyncio.wait_for(entered.wait(), 1)
        await stream.aclose()
        await asyncio.wait_for(cancelled.wait(), 1)
        await self.wait_idle(session)
        self.assertFalse(session._detached_tasks)

    async def test_cancel_during_admission_records_request_without_publishing_a_turn(self):
        from app.agent.kernel.state_types import AgentInput

        entered = asyncio.Event()

        class PendingAdmission:
            async def begin(self, agent_input):
                entered.set()
                await asyncio.Event().wait()

        session = make_session()
        session.turn_admission = cast(TurnAdmissionPolicy, PendingAdmission())
        stream = session.run(AgentInput(
            owner="owner", session_id="session", request_id="admission-request",
            message="尚未准入", channel="api",
        ))
        observer = asyncio.create_task(anext(stream))
        await asyncio.wait_for(entered.wait(), 1)
        observer.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await observer
        await stream.aclose()
        metrics = session.metrics.snapshot()
        self.assertEqual(metrics["statuses"], {"cancelled": 1})
        self.assertEqual(metrics["recent"][0]["request_id"], "admission-request")
        self.assertEqual(metrics["recent"][0]["model_calls"], 0)
        self.assertEqual(metrics["task_statuses_by_turn"], {})
        state = await session.state_store.load(owner="owner", session_id="session")
        self.assertFalse(state.conversation)

    async def test_revoked_admission_is_counted_as_cancelled_not_failed(self):
        from app.agent.kernel.state_types import AgentInput, StalePublicationError

        class RevokedAdmission:
            async def begin(self, agent_input):
                raise StalePublicationError("revoked")

        session = make_session()
        session.turn_admission = cast(TurnAdmissionPolicy, RevokedAdmission())
        events = [event async for event in session.run(AgentInput(
            owner="owner", session_id="session", request_id="revoked-request",
            message="已被撤销", channel="api",
        ))]
        self.assertEqual(events, [])
        snapshot = session.metrics.snapshot()
        self.assertEqual(snapshot["statuses"], {"cancelled": 1})
        self.assertEqual(snapshot["recent"][0]["request_id"], "revoked-request")

    async def test_web_new_turn_still_blocks_old_turn_late_publication(self):
        session, entered, release, cancelled, calls = self.paused_session()
        web = WebKernelTransport(session)
        stream = web.query(QueryEnvelope(owner="owner", session_id="session", request_id="first", message="第一条"))
        await anext(stream)
        await asyncio.wait_for(entered.wait(), 1)
        await stream.aclose()
        session.model = ReadThenAnswerModel()
        events = [json.loads(chunk) async for chunk in web.query(QueryEnvelope(owner="owner", session_id="session", request_id="second", message="媒体库有多少集"))]
        self.assertEqual(events[-1]["payload"]["answer"], "媒体库共有 37 集")
        await asyncio.wait_for(cancelled.wait(), 1)
        self.assertFalse(release.is_set())  # 顶替必须主动中断旧模型等待。
        release.set()
        await self.wait_idle(session)
        state = await session.state_store.load(owner="owner", session_id="session")
        self.assertEqual(state.conversation[-1]["content"], "媒体库共有 37 集")
        self.assertNotIn("离开页面后仍完成", str(state.conversation))
