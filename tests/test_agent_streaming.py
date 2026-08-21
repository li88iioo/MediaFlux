"""Media Agent Web 流式路由合同与历史终态测试。"""
from __future__ import annotations

import asyncio
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app.agent.operation_coordinator import (
    get_agent_operation_coordinator,
    reset_agent_operation_state_for_tests,
)
from app.agent.rate_limit import agent_rate_limiter
from app.clients.openai_compatible import ProviderStreamError
from app.config import web_credentials
from app.main import create_app
from app.routes.agent_api import _stream_query_events
from tests.support import IsolatedDatabaseTestCase


SESSION_ID = "agent_stream_session_0001"


def _tool_response() -> dict:
    return {
        "request_id": "stream-request",
        "mode": "read_only",
        "tool_call": {
            "name": "downloads.diagnose_queue",
            "arguments": {},
            "elapsed_ms": 3,
        },
        "result": {
            "ok": True,
            "status": "healthy",
            "summary": "下载队列状态正常",
            "error": "",
            "suggestions": [],
            "data": {"total": 16},
            "evidence": [],
        },
    }


class _FakeService:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.calls: list[tuple[str, dict]] = []

    def query(self, message: str, **kwargs):
        self.calls.append((message, kwargs))
        return self.response


class _BlockingService(_FakeService):
    def __init__(self, response: dict) -> None:
        super().__init__(response)
        self.started = threading.Event()
        self.release = threading.Event()

    def query(self, message: str, **kwargs):
        self.calls.append((message, kwargs))
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("测试未释放阻塞查询")
        return self.response


class AgentStreamingApiTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        agent_rate_limiter.reset()
        reset_agent_operation_state_for_tests()
        self.client = TestClient(
            create_app(start_background=False), raise_server_exceptions=False
        )

    def tearDown(self) -> None:
        self.client.close()
        agent_rate_limiter.reset()
        reset_agent_operation_state_for_tests()

    @staticmethod
    def _csrf(html: str) -> str:
        matched = re.search(
            r'name="csrf_token"\s+(?:value|content)="([^"]+)"', html
        ) or re.search(r'name="csrf-token" content="([^"]+)"', html)
        if not matched:
            raise AssertionError("页面未输出 CSRF Token")
        return matched.group(1)

    def _login(self) -> str:
        page = self.client.get("/login")
        username, password = web_credentials()
        response = self.client.post(
            "/login",
            data={
                "username": username,
                "password": password,
                "csrf_token": self._csrf(page.text),
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302, response.text)
        return self._csrf(self.client.get("/agent").text)

    @staticmethod
    def _events(response) -> list[dict]:
        return [json.loads(line) for line in response.text.splitlines() if line.strip()]

    def test_query_streams_deltas_and_persists_only_final_payload(self):
        csrf = self._login()
        service = _FakeService(_tool_response())
        history = Mock()

        async def stream_answer(*_args, **_kwargs):
            yield "下载队列"
            yield "目前正常，共 16 项任务。"

        with (
            patch("app.routes.agent_api.get_agent_service", return_value=service),
            patch("app.routes.agent_api.stream_tool_answer", stream_answer),
            patch("app.routes.agent_api._record_query_history", history),
        ):
            response = self.client.post(
                "/api/agent/query",
                headers={"X-CSRF-Token": csrf},
                json={
                    "message": "检查下载队列状态",
                    "session_id": SESSION_ID,
                    "stream": True,
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(service.calls), 1)
        call_kwargs = service.calls[0][1]
        self.assertEqual(
            call_kwargs["query_tool_rate_identity"],
            call_kwargs["llm_tool_rate_identity"],
        )
        self.assertTrue(
            call_kwargs["query_tool_rate_identity"].startswith("web-tool-rate:v1:")
        )
        self.assertTrue(call_kwargs["llm_rate_owner"].startswith("web-rate:v1:"))
        self.assertIn("application/x-ndjson", response.headers["content-type"])
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-accel-buffering"], "no")
        events = self._events(response)
        self.assertEqual(
            [item["type"] for item in events],
            ["status", "status", "status", "delta", "final"],
        )
        self.assertEqual(
            [item["phase"] for item in events[:3]],
            ["routing", "reviewing", "answering"],
        )
        self.assertTrue(all(item.get("request_id") for item in events))
        final = events[-1]["payload"]
        self.assertEqual(
            final["presentation"]["narrative"],
            "下载队列目前正常，共 16 项任务。",
        )
        self.assertEqual(final["result"]["summary"], "下载队列状态正常")
        self.assertFalse(service.calls[0][1]["present"])
        history.assert_called_once()
        self.assertEqual(history.call_args.kwargs["response"], final)

    def test_partial_stream_failure_before_a_complete_sentence_falls_back_safely(self):
        csrf = self._login()
        service = _FakeService(_tool_response())
        history = Mock()

        async def broken_stream(*_args, **_kwargs):
            yield "当前已生成一部分"
            raise ProviderStreamError("upstream interrupted")

        with (
            patch("app.routes.agent_api.get_agent_service", return_value=service),
            patch("app.routes.agent_api.stream_tool_answer", broken_stream),
            patch("app.routes.agent_api._record_query_history", history),
        ):
            response = self.client.post(
                "/api/agent/query",
                headers={"X-CSRF-Token": csrf},
                json={
                    "message": "检查下载队列状态",
                    "session_id": SESSION_ID,
                    "stream": True,
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        events = self._events(response)
        self.assertEqual(
            [item["type"] for item in events],
            ["status", "status", "status", "final"],
        )
        final = events[-1]["payload"]
        self.assertEqual(final["result"]["summary"], "下载队列状态正常")
        self.assertNotIn("当前已生成一部分", response.text)
        history.assert_called_once()

    def test_partial_stream_interruption_persists_public_prefix_and_tool_state(self):
        csrf = self._login()
        service = _FakeService(_tool_response())
        history = Mock()

        async def broken_stream(*_args, **_kwargs):
            yield "下载队列已完成检查。"
            raise ProviderStreamError("upstream interrupted")

        with (
            patch("app.routes.agent_api.get_agent_service", return_value=service),
            patch("app.routes.agent_api.stream_tool_answer", broken_stream),
            patch("app.routes.agent_api._record_query_history", history),
        ):
            response = self.client.post(
                "/api/agent/query",
                headers={"X-CSRF-Token": csrf},
                json={
                    "message": "检查下载队列状态",
                    "session_id": SESSION_ID,
                    "stream": True,
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        events = self._events(response)
        self.assertEqual(
            [item["type"] for item in events],
            ["status", "status", "status", "delta", "error"],
        )
        self.assertEqual(events[-1]["code"], "stream_interrupted")
        history.assert_called_once()
        saved = history.call_args.kwargs["response"]
        self.assertEqual(saved["result"]["status"], "interrupted")
        self.assertEqual(saved["result"]["summary"], "下载队列已完成检查。")
        self.assertEqual(saved["tool_call"]["name"], "downloads.diagnose_queue")
        self.assertEqual(saved["presentation"]["status"], "interrupted")

    def test_split_unsafe_stream_token_is_never_publicly_emitted(self):
        csrf = self._login()
        service = _FakeService(_tool_response())
        history = Mock()

        async def unsafe_stream(*_args, **_kwargs):
            yield "已完成基础检查。请访问 https://"
            yield "private.invalid/result"

        with (
            patch("app.routes.agent_api.get_agent_service", return_value=service),
            patch("app.routes.agent_api.stream_tool_answer", unsafe_stream),
            patch("app.routes.agent_api._record_query_history", history),
        ):
            response = self.client.post(
                "/api/agent/query",
                headers={"X-CSRF-Token": csrf},
                json={
                    "message": "检查下载队列状态",
                    "session_id": SESSION_ID,
                    "stream": True,
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        events = self._events(response)
        self.assertEqual(
            [item["type"] for item in events],
            ["status", "status", "status", "delta", "error"],
        )
        self.assertEqual(events[-1]["code"], "stream_invalid")
        self.assertEqual(events[3]["delta"], "已完成基础检查。")
        self.assertNotIn("https://", response.text)
        self.assertNotIn("private.invalid", response.text)
        history.assert_not_called()

    def test_windows_path_stream_token_is_never_publicly_emitted(self):
        csrf = self._login()
        service = _FakeService(_tool_response())
        history = Mock()

        async def unsafe_stream(*_args, **_kwargs):
            yield "已完成基础检查。路径 "
            yield r"\Windows\System32\config"

        with (
            patch("app.routes.agent_api.get_agent_service", return_value=service),
            patch("app.routes.agent_api.stream_tool_answer", unsafe_stream),
            patch("app.routes.agent_api._record_query_history", history),
        ):
            response = self.client.post(
                "/api/agent/query",
                headers={"X-CSRF-Token": csrf},
                json={
                    "message": "检查下载队列状态",
                    "session_id": SESSION_ID,
                    "stream": True,
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        events = self._events(response)
        self.assertEqual(
            [item["type"] for item in events],
            ["status", "status", "status", "delta", "error"],
        )
        self.assertEqual(events[-1]["code"], "stream_invalid")
        self.assertEqual(events[3]["delta"], "已完成基础检查。")
        self.assertNotIn("Windows", response.text)
        self.assertNotIn("System32", response.text)
        history.assert_not_called()

    def test_confirmation_response_never_enters_provider_stream(self):
        csrf = self._login()
        response_payload = _tool_response()
        response_payload["mode"] = "confirmation_required"
        response_payload["confirmation"] = {"confirmation_id": "confirmation-1"}
        service = _FakeService(response_payload)

        async def should_not_stream(*_args, **_kwargs):
            raise AssertionError("确认类响应不得进入 Provider 流")
            yield ""

        with (
            patch("app.routes.agent_api.get_agent_service", return_value=service),
            patch("app.routes.agent_api.stream_tool_answer", should_not_stream),
        ):
            response = self.client.post(
                "/api/agent/query",
                headers={"X-CSRF-Token": csrf},
                json={"session_id": "test_session_identifier_0001", "message": "准备执行操作", "stream": True},
            )

        events = self._events(response)
        self.assertEqual(
            [item["type"] for item in events],
            ["status", "status", "final"],
        )
        self.assertEqual(events[-1]["payload"]["mode"], "confirmation_required")

    def test_zero_delta_falls_back_to_deterministic_final(self):
        csrf = self._login()
        service = _FakeService(_tool_response())

        async def empty_stream(*_args, **_kwargs):
            if False:
                yield ""

        with (
            patch("app.routes.agent_api.get_agent_service", return_value=service),
            patch("app.routes.agent_api.stream_tool_answer", empty_stream),
        ):
            response = self.client.post(
                "/api/agent/query",
                headers={"X-CSRF-Token": csrf},
                json={"session_id": "test_session_identifier_0001", "message": "检查下载队列状态", "stream": True},
            )

        events = self._events(response)
        self.assertEqual(
            [item["type"] for item in events],
            ["status", "status", "status", "final"],
        )
        self.assertNotIn("presentation", events[-1]["payload"])

    def test_cancel_arriving_before_stream_prevents_query_and_history(self):
        csrf = self._login()
        request_id = "request_cancel_0001"
        service = _FakeService(_tool_response())
        history = Mock()

        cancel = self.client.post(
            "/api/agent/query/cancel",
            headers={"X-CSRF-Token": csrf},
            json={"request_id": request_id, "session_id": SESSION_ID},
        )
        self.assertEqual(cancel.status_code, 200, cancel.text)
        self.assertEqual(cancel.json()["request_id"], request_id)
        self.assertTrue(cancel.json()["cancelled"])

        with (
            patch("app.routes.agent_api.get_agent_service", return_value=service),
            patch("app.routes.agent_api._record_query_history", history),
        ):
            response = self.client.post(
                "/api/agent/query",
                headers={"X-CSRF-Token": csrf},
                json={
                    "message": "检查下载队列状态",
                    "session_id": SESSION_ID,
                    "stream": True,
                    "request_id": request_id,
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        events = self._events(response)
        self.assertEqual([item["type"] for item in events], ["cancelled"])
        self.assertEqual(events[-1]["request_id"], request_id)
        self.assertEqual(service.calls, [])
        history.assert_not_called()

    def test_cancel_during_slow_query_revokes_final_and_history(self):
        csrf = self._login()
        request_id = "request_cancel_active_0001"
        service = _BlockingService(_tool_response())
        history = Mock()
        cancel_client = TestClient(
            create_app(start_background=False), raise_server_exceptions=False
        )
        cancel_client.cookies.update(self.client.cookies)

        def execute_query():
            return self.client.post(
                "/api/agent/query",
                headers={"X-CSRF-Token": csrf},
                json={
                    "message": "检查下载队列状态",
                    "session_id": SESSION_ID,
                    "stream": True,
                    "request_id": request_id,
                },
            )

        try:
            with (
                patch("app.routes.agent_api.get_agent_service", return_value=service),
                patch("app.routes.agent_api._record_query_history", history),
                ThreadPoolExecutor(max_workers=1) as pool,
            ):
                future = pool.submit(execute_query)
                self.assertTrue(service.started.wait(timeout=3), "慢查询未进入执行阶段")
                cancel = cancel_client.post(
                    "/api/agent/query/cancel",
                    headers={"X-CSRF-Token": csrf},
                    json={"request_id": request_id, "session_id": SESSION_ID},
                )
                self.assertEqual(cancel.status_code, 200, cancel.text)
                self.assertTrue(cancel.json()["cancelled"])
                service.release.set()
                response = future.result(timeout=5)
        finally:
            service.release.set()
            cancel_client.close()

        self.assertEqual(response.status_code, 200, response.text)
        events = self._events(response)
        self.assertEqual([item["type"] for item in events], ["status", "cancelled"])
        self.assertEqual(events[-1]["reason"], "user_cancelled")
        self.assertNotIn('"type":"final"', response.text)
        history.assert_not_called()


    def test_stream_generator_publishes_cancel_before_worker_thread_finishes(self):
        service = _BlockingService(_tool_response())
        coordinator = get_agent_operation_coordinator()
        owner = "web:v1:test-stream-cancel"
        request_id = "request_cancel_generator_0001"

        class RequestStub:
            async def is_disconnected(self):
                return False

        async def scenario():
            operation, _generation = coordinator.begin_with_context(
                owner=owner,
                operation_id=request_id,
                initialize=lambda: None,
            )
            events = _stream_query_events(
                RequestStub(),
                message="检查下载队列状态",
                service=service,
                query_kwargs={
                    "owner": owner,
                    "llm_rate_owner": "web-rate:v1:test",
                    "query_tool_rate_identity": "web-tool-rate:v1:test",
                    "llm_tool_rate_identity": "web-tool-rate:v1:test",
                },
                llm_owner="web-rate:v1:test",
                session_id=None,
                history_generation=None,
                operation=operation,
            )
            first = json.loads((await anext(events)).decode("utf-8"))
            pending = asyncio.create_task(anext(events))
            started = await asyncio.to_thread(service.started.wait, 1)
            self.assertTrue(started, "慢查询未进入执行阶段")
            self.assertTrue(
                coordinator.cancel(
                    owner=owner,
                    operation_id=request_id,
                    reason="user_cancelled",
                    remember=True,
                )
            )
            second = json.loads(
                (await asyncio.wait_for(pending, timeout=1)).decode("utf-8")
            )
            self.assertFalse(
                service.release.is_set(),
                "撤销事件不应等待同步工具线程自行结束",
            )
            service.release.set()
            await asyncio.sleep(0)
            await events.aclose()
            return first, second

        try:
            first, second = asyncio.run(scenario())
        finally:
            service.release.set()

        self.assertEqual(first["type"], "status")
        self.assertEqual(second["type"], "cancelled")
        self.assertEqual(second["reason"], "user_cancelled")

    def test_request_id_is_validated_for_query_and_cancel(self):
        csrf = self._login()
        for endpoint, payload in (
            ("/api/agent/query", {"session_id": "test_session_identifier_0001", "message": "检查下载队列状态", "request_id": "bad id"}),
            ("/api/agent/query/cancel", {"session_id": "test_session_identifier_0001", "request_id": "bad id"}),
        ):
            with self.subTest(endpoint=endpoint):
                response = self.client.post(
                    endpoint,
                    headers={"X-CSRF-Token": csrf},
                    json=payload,
                )
                self.assertEqual(response.status_code, 400, response.text)
                self.assertIn("request_id 无效", response.text)

    def test_stream_field_must_be_boolean(self):
        csrf = self._login()
        response = self.client.post(
            "/api/agent/query",
            headers={"X-CSRF-Token": csrf},
            json={"session_id": "test_session_identifier_0001", "message": "检查下载队列状态", "stream": "yes"},
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("stream 必须是布尔值", response.text)
