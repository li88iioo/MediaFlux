"""Offline native wire -> real Session -> SQLite -> actual transport boundaries."""

from typing import cast
from collections.abc import Callable
from app.clients.http import FixedHostHttpClient

import asyncio
import json
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.kernel.adapters import consume_events
from app.agent.kernel.capabilities import (
    CapabilityRetriever,
    KernelToolSpec,
    ToolCatalog,
    ToolEffect,
)
from app.agent.kernel.effects import ConfirmationEffectPlanStore, PreparedEffect
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.provider_model import (
    OpenAICompatibleModelAdapter,
    ProviderSettings,
)
from app.agent.kernel.session import AgentSession
from app.agent.kernel.state_types import AgentInput
from app.agent.transports import TelegramKernelTransport, WebKernelTransport
from tests.support import initialized_web_runtime, isolated_test_database


class NativeRuntime:
    def __init__(self):
        self.wires = []
        self.writes = []
        self.prepares = []
        self.now = 1000.0
        self.force_call = False
        self.once_call = False
        self.gate = None
        self.call_arguments = '{"step":1}'
        self.call_name = None
        self.store = SQLiteKernelStore()
        self.effects = ConfirmationEffectPlanStore(
            SQLiteConfirmationStore(clock=lambda: self.now)
        )

        def prepare(arguments, context):
            self.prepares.append(dict(arguments))
            return PreparedEffect(
                preview={"summary": "改名一个对象"}, snapshot_fingerprint="frozen-1"
            )

        tool = KernelToolSpec(
            name="cloud.change",
            domain="cloud",
            description="改名",
            input_schema={
                "type": "object",
                "properties": {"step": {"type": "integer"}},
                "required": ["step"],
            },
            effect=ToolEffect.WRITE,
            prepare=prepare,
            execute_confirmed=lambda a, s, c: (
                self.writes.append((dict(a), s)) or {"ok": True, "summary": "已改名"}
            ),
        )
        self.catalog = ToolCatalog([tool])

        async def handler(request):
            body = json.loads(request.content)
            self.wires.append(body)
            if self.gate is not None and len(self.wires) == 2:
                self.gate[0].set()
                await self.gate[1].wait()
            if len(self.wires) == 1 or self.force_call or self.once_call:
                self.once_call = False
                delta = {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "native-" + str(len(self.wires)),
                            "type": "function",
                            "function": {
                                "name": self.call_name or tool.model_name,
                                "arguments": self.call_arguments,
                            },
                        }
                    ]
                }
                finish = "tool_calls"
            else:
                delta = {"content": "解释：该计划只影响一个对象，尚未执行。"}
                finish = "stop"
            raw = (
                "data: "
                + json.dumps({"choices": [{"delta": delta, "finish_reason": finish}]})
                + "\n\ndata: [DONE]\n\n"
            )
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=raw.encode()
            )

        class Client:
            def __init__(self):
                self.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))

            @asynccontextmanager
            async def stream_post_json(self, url, **kwargs):
                async with self.http.stream(
                    "POST", url, json=kwargs["json"], headers=kwargs.get("headers")
                ) as response:
                    yield response

            async def aclose(self):
                await self.http.aclose()

        self.model = OpenAICompatibleModelAdapter(
            ProviderSettings(
                api_url="https://example.invalid/v1",
                model="fixture",
                protocol="chat_completions",
            ),
            client_factory=cast(Callable[..., FixedHostHttpClient], lambda **_: Client()),
        )
        self.reopen()

    def reopen(self):
        self.store = SQLiteKernelStore()
        self.session = AgentSession(
            model=self.model,
            catalog=self.catalog,
            retriever=CapabilityRetriever(),
            pipeline=ToolPipeline(
                catalog=self.catalog, state_store=self.store, effect_store=self.effects
            ),
            state_store=self.store,
            journal=self.store,
        )
        self.web = WebKernelTransport(self.session)
        self.telegram = TelegramKernelTransport(self.session)

    def preview(self, owner, session_id):
        view = asyncio.run(
            consume_events(
                self.session.run(
                    AgentInput(
                        message="改名一个对象", owner=owner, session_id=session_id
                    )
                )
            )
        )
        assert view.approval is not None, view
        return view.approval


@contextmanager
def api_client(runtime, monkeypatch, csrf="explain-csrf"):
    with initialized_web_runtime():
        from app.main import SecurityMiddleware
        from app.routes import agent_api

        application = FastAPI()
        application.include_router(agent_api.router)
        application.add_middleware(SecurityMiddleware)
        application.add_middleware(SessionMiddleware, secret_key="explain-test-secret")

        @application.get("/_test_login")
        def login(request: Request):
            request.session.update(logged_in=True, csrf_token=csrf)
            return {"ok": True}

        monkeypatch.setattr(
            agent_api.config, "web_credentials", lambda: ("explain-account", "unused")
        )
        monkeypatch.setattr(agent_api, "get_agent_kernel_runtime", lambda: runtime)
        monkeypatch.setattr(agent_api, "is_agent_enabled", lambda: True)
        with TestClient(application, raise_server_exceptions=False) as client:
            yield client, agent_api.web_kernel_owner("explain-account")


def test_confirm_rate_limit_preserves_pending_ticket_for_readonly_recovery(monkeypatch):
    from app.routes import agent_api

    with isolated_test_database("confirm-rate-limit.db"):
        runtime = NativeRuntime()
        with api_client(runtime, monkeypatch) as (client, owner):
            client.get("/_test_login")
            session_id = "session_confirm_rate_limit_001"
            approval = runtime.preview(owner, session_id)
            before = asyncio.run(runtime.session.pipeline.get_pending_plan(owner=owner, session_id=session_id))
            assert before is not None
            for _ in range(12):
                agent_api._check_rate_limit("confirm", limit=12)
            request_id = "confirm_rate_limit_rejected"
            response = client.post("/api/agent/actions/confirm", json={
                "session_id": session_id, "plan_id": approval.plan_id, "request_id": request_id, "stream": True,
            }, headers={"X-CSRF-Token": "explain-csrf"})
            assert response.status_code == 429
            assert "请求过于频繁" in response.json()["error"]
            assert asyncio.run(runtime.session.pipeline.get_pending_plan(owner=owner, session_id=session_id)) == before
            observed = {"X-Agent-Request-Id": request_id}
            snapshot = client.get(f"/api/agent/sessions/{session_id}", headers=observed)
            assert snapshot.status_code == 200
            payload = snapshot.json()
            assert payload["session_id"] == session_id
            assert payload["pending_approval"]["plan_id"] == approval.plan_id
            assert payload["active_turn"] is None and payload["last_turn"] is None
            assert "no-store" in snapshot.headers["Cache-Control"]
            runtime.now = before.expires_at + 1
            expired = client.get(f"/api/agent/sessions/{session_id}", headers=observed)
            assert expired.status_code == 200 and expired.json()["pending_approval"] is None
            assert runtime.writes == [] and len(runtime.prepares) == 1 and len(runtime.wires) == 1


def test_web_security_cold_confirm_and_tool_rejection(monkeypatch):
    with isolated_test_database("explain-web.db"):
        runtime = NativeRuntime()
        with api_client(runtime, monkeypatch) as (client, owner):
            session_id = "session_explanation_001"
            approval = runtime.preview(owner, session_id)
            plan = asyncio.run(
                runtime.session.pipeline.get_pending_plan(
                    owner=owner, session_id=session_id
                )
            )
            body = {
                "session_id": session_id,
                "plan_id": approval.plan_id,
                "message": "解释；也执行吧",
                "stream": False,
            }
            assert client.post("/api/agent/actions/explain", json=body).status_code in (
                401,
                403,
            )
            client.get("/_test_login")
            assert (
                client.post("/api/agent/actions/explain", json=body).status_code == 403
            )
            headers = {"X-CSRF-Token": "explain-csrf"}
            assert (
                client.post(
                    "/api/agent/actions/explain",
                    json={**body, "generation": 1},
                    headers=headers,
                ).status_code
                == 400
            )
            response = client.post(
                "/api/agent/actions/explain", json=body, headers=headers
            )
            assert response.status_code == 200, response.text
            assert runtime.writes == [] and len(runtime.prepares) == 1
            assert not runtime.wires[-1].get("tools")
            runtime.reopen()
            assert (
                asyncio.run(
                    runtime.session.pipeline.get_pending_plan(
                        owner=owner, session_id=session_id
                    )
                )
                == plan
            )
            cold = client.get("/api/agent/sessions/" + session_id)
            assert cold.status_code == 200, cold.text
            assert cold.json()["pending_approval"]["plan_id"] == approval.plan_id
            runtime.force_call = True
            rejected = client.post(
                "/api/agent/actions/explain", json=body, headers=headers
            )
            assert rejected.status_code == 409, rejected.text
            assert rejected.json()["error"]["code"] == "explanation_tools_forbidden"
            assert runtime.writes == [] and len(runtime.prepares) == 1
            runtime.force_call = False
            runtime.once_call = True
            confirmed = client.post(
                "/api/agent/actions/confirm",
                json={k: v for k, v in body.items() if k != "message"},
                headers=headers,
            )
            assert confirmed.status_code == 200, confirmed.text
            client.post(
                "/api/agent/actions/confirm",
                json={k: v for k, v in body.items() if k != "message"},
                headers=headers,
            )
            assert runtime.writes == [({"step": 1}, "frozen-1")]
            assert len(runtime.prepares) == 1
            final_system = runtime.wires[-1]["messages"][0]["content"]
            assert '原任务目标（后续解释问题不是新任务）："改名一个对象"' in final_system


def test_telegram_actual_callback_preserves_card(monkeypatch):
    from app.bot import agent_adapter
    from tests.test_agent_kernel_telegram_adapter import TELEBOT

    with isolated_test_database("explain-telegram.db"):
        runtime = NativeRuntime()
        owner = agent_adapter.telegram_agent_owner("123", "456")
        session_id = agent_adapter.telegram_agent_session_id("123", "456", 7)
        approval = runtime.preview(owner, session_id)
        monkeypatch.setattr(agent_adapter, "get_agent_kernel_runtime", lambda: runtime)
        get_config = agent_adapter.config.get
        settings = {
            "TG_CHAT_ID": "123",
            "TG_AGENT_ALLOWED_USER_IDS": "456",
            "TG_AGENT_ENABLED": "1",
        }
        monkeypatch.setattr(
            agent_adapter.config,
            "get",
            lambda key, default="": settings.get(key, get_config(key, default)),
        )
        monkeypatch.setattr(agent_adapter, "is_agent_enabled", lambda: True)
        bot = Mock()
        markup = agent_adapter._approval_markup(
            TELEBOT, approval, owner=owner, session_id=session_id, message_thread_id=7
        )
        callbacks = [button.callback_data for button in markup.buttons]
        assert len(callbacks) == 3
        message = SimpleNamespace(
            chat=SimpleNamespace(id=123),
            message_id=7,
            message_thread_id=7,
            reply_markup=markup,
        )
        call = SimpleNamespace(
            id="explain-cb",
            data=next(value for value in callbacks if value.startswith("agk:e:")),
            message=message,
            from_user=SimpleNamespace(id=456),
        )
        agent_adapter.handle_agent_callback(bot, call)
        assert bot.send_message.called
        assert (
            not bot.edit_message_text.called
            and not bot.edit_message_reply_markup.called
        )
        assert [
            button.callback_data for button in message.reply_markup.buttons
        ] == callbacks
        assert (
            runtime.writes == []
            and len(runtime.prepares) == 1
            and len(runtime.wires) == 2
        )
        assert not runtime.wires[-1].get("tools")
        call.from_user.id = 999
        agent_adapter.handle_agent_callback(bot, call)
        assert len(runtime.wires) == 2
        call.from_user.id = 456
        message.message_thread_id = 8
        agent_adapter.handle_agent_callback(bot, call)
        assert len(runtime.wires) == 2
        message.message_thread_id = 7
        runtime.reopen()
        view = asyncio.run(
            consume_events(
                runtime.session.confirm(
                    owner=owner, session_id=session_id, plan_id=approval.plan_id
                )
            )
        )
        assert view.status == "success"
        assert runtime.writes == [({"step": 1}, "frozen-1")]
