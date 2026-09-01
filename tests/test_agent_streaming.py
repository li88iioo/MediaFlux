"""Media Agent Web 流式路由合同与历史终态测试。"""
from __future__ import annotations

import asyncio
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app.agent.confirmation import ConfirmationStore
from app.agent.operation_coordinator import (
    get_agent_operation_coordinator,
    reset_agent_operation_state_for_tests,
)
from app.agent.rate_limit import agent_rate_limiter
from app.agent.state_commit import commit_or_defer_agent_state
from app.clients.openai_compatible import ProviderStreamError
from app.config import web_credentials
from app.main import create_app
from app.routes.agent_api import (
    _public_deterministic_fallback_response,
    _stream_query_events,
)
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
        self.confirmation_epoch = 0

    def begin_query_confirmation_epoch(self, *, owner: str) -> int:
        del owner
        self.confirmation_epoch += 1
        return self.confirmation_epoch

    def invalidate_query_confirmation_epoch(self, *, owner: str) -> int:
        del owner
        self.confirmation_epoch += 1
        return self.confirmation_epoch

    def query(self, message: str, **kwargs):
        self.calls.append((message, kwargs))
        return self.response


class _StatefulFakeService(_FakeService):
    def __init__(self, response: dict) -> None:
        super().__init__(response)
        self.state_commits: list[str] = []

    def query(self, message: str, **kwargs):
        self.calls.append((message, kwargs))
        commit_or_defer_agent_state(
            lambda: self.state_commits.append(message)
        )
        return self.response


class _BlockingService(_FakeService):
    def __init__(self, response: dict) -> None:
        super().__init__(response)
        self.started = threading.Event()
        self.release = threading.Event()
        self.state_commits: list[str] = []

    def query(self, message: str, **kwargs):
        self.calls.append((message, kwargs))
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("测试未释放阻塞查询")
        commit_or_defer_agent_state(
            lambda: self.state_commits.append(message)
        )
        return self.response


class _BlockingInvokeService(_FakeService):
    def __init__(self, response: dict) -> None:
        super().__init__(response)
        self.started = threading.Event()
        self.release = threading.Event()
        self.state_commits: list[str] = []

    @staticmethod
    def has_tool(_tool_name: str) -> bool:
        return True

    @staticmethod
    def is_read_tool(_tool_name: str) -> bool:
        return True

    def invoke(self, tool_name: str, arguments: dict, **_kwargs):
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("测试未释放阻塞工具")
        commit_or_defer_agent_state(
            lambda: self.state_commits.append(tool_name)
        )
        return self.response


class _ConfirmationRaceService(_FakeService):
    def __init__(self, response: dict) -> None:
        super().__init__(response)
        self.confirmation_store = ConfirmationStore()
        self.started = threading.Event()
        self.release = threading.Event()
        self.owner = ""

    @staticmethod
    def has_tool(_tool_name: str) -> bool:
        return True

    @staticmethod
    def is_read_tool(_tool_name: str) -> bool:
        return True

    def begin_query_confirmation_epoch(self, *, owner: str) -> int:
        self.owner = owner
        _revoked, generation = self.confirmation_store.rotate_owner(
            owner=owner, preserve_active=True
        )
        return generation

    def invalidate_query_confirmation_epoch(self, *, owner: str) -> int:
        revoked, _generation = self.confirmation_store.rotate_owner(owner=owner)
        return revoked

    def query(self, message: str, **kwargs):
        self.calls.append((message, kwargs))
        self.owner = str(kwargs.get("owner") or "")
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("测试未释放确认查询")
        self.confirmation_store.issue(
            owner=self.owner,
            tool_name="test.write",
            arguments={"enabled": True},
            expected_owner_generation=kwargs.get("confirmation_owner_generation"),
        )
        return self.response

    def invoke(self, _tool_name: str, _arguments: dict, **_kwargs):
        return self.response

    def invoke_workspace_action(self, _action_key: str, **_kwargs):
        return self.response


class _HiddenConfirmationService(_FakeService):
    def __init__(self) -> None:
        super().__init__(_tool_response())
        self.confirmation_store = ConfirmationStore()
        self.started = threading.Event()
        self.release = threading.Event()
        self.owner = ""

    @staticmethod
    def has_tool(_tool_name: str) -> bool:
        return True

    @staticmethod
    def confirmation_followup_target(_tool_name: str) -> str:
        return ""

    @staticmethod
    def active_confirmation_count(*, owner: str) -> int:
        del owner
        return 0

    def begin_query_confirmation_epoch(self, *, owner: str) -> int:
        _revoked, generation = self.confirmation_store.rotate_owner(
            owner=owner, preserve_active=True
        )
        return generation

    def invalidate_query_confirmation_epoch(self, *, owner: str) -> int:
        revoked, _generation = self.confirmation_store.rotate_owner(owner=owner)
        return revoked

    def discard_confirmation(
        self, confirmation_id: str, *, owner: str, advance_owner_epoch: bool = True
    ) -> bool:
        del advance_owner_epoch
        return self.confirmation_store.discard(
            owner=owner, confirmation_id=confirmation_id
        )

    def _issue_response(self, *, owner: str, generation: int | None) -> dict:
        ticket = self.confirmation_store.issue(
            owner=owner,
            tool_name="test.write",
            arguments={"enabled": True},
            expected_owner_generation=generation,
            replace_active_ticket=True,
        )
        return {
            "mode": "confirmation_required",
            "tool_call": {"name": "test.write", "elapsed_ms": 1},
            "result": {
                "ok": True,
                "status": "confirmation_required",
                "summary": "等待确认",
                "suggestions": [],
                "evidence": [],
            },
            "action_plan": {
                "version": 1,
                "plan_id": ticket.confirmation_id,
                "status": "awaiting_approval",
                "title": "测试写入",
                "target": "测试对象",
                "impact": "会执行测试写入",
                "reversibility": "可恢复",
                "risk": "write",
                "preflight_at": "2026-09-01T00:00:00+08:00",
                "preflight_summary": "预检完成",
                "expires_in": 60,
                "decisions": [
                    {"id": "execute", "label": "执行"},
                    {"id": "cancel", "label": "取消"},
                ],
            },
        }

    def prepare(self, _tool_name: str, _arguments: dict, **kwargs):
        self.owner = str(kwargs.get("owner") or "")
        response = self._issue_response(
            owner=self.owner,
            generation=kwargs.get("expected_owner_generation"),
        )
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("测试未释放确认预检")
        return response

    def query(self, message: str, **kwargs):
        self.calls.append((message, kwargs))
        if message != "旧确认请求":
            return self.response
        self.owner = str(kwargs.get("owner") or "")
        response = self._issue_response(
            owner=self.owner,
            generation=kwargs.get("confirmation_owner_generation"),
        )
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("测试未释放确认查询")
        return response


class _BlockingWorkspaceService(_FakeService):
    def __init__(self, response: dict) -> None:
        super().__init__(response)
        self.started = threading.Event()
        self.release = threading.Event()
        self.state_commits: list[str] = []

    def invoke_workspace_action(self, action_key: str, **_kwargs):
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("测试未释放工作区行动")
        commit_or_defer_agent_state(
            lambda: self.state_commits.append(action_key)
        )
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
        self.assertEqual(final["presentation"]["source"], "system")
        self.assertEqual(final["presentation"]["narrative"], "下载队列状态正常")
        self.assertTrue(final["presentation"]["degraded"])
        self.assertNotIn("当前已生成一部分", response.text)
        history.assert_called_once()
        self.assertEqual(
            history.call_args.kwargs["response"]["presentation"],
            final["presentation"],
        )

    def test_partial_stream_interruption_persists_public_prefix_and_tool_state(self):
        csrf = self._login()
        service = _StatefulFakeService(_tool_response())
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
        self.assertEqual(service.state_commits, ["检查下载队列状态"])

    def test_runtime_change_during_partial_stream_discards_state_and_history(self):
        csrf = self._login()
        service = _StatefulFakeService(_tool_response())
        history = Mock()

        async def broken_stream(*_args, **_kwargs):
            from app.agent.feature_gate import invalidate_agent_runtime_generation

            yield "下载队列已完成检查。"
            invalidate_agent_runtime_generation()
            raise ProviderStreamError("runtime changed")

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
            ["status", "status", "status", "delta", "cancelled"],
        )
        self.assertEqual(events[-1]["reason"], "runtime_changed")
        history.assert_not_called()
        self.assertEqual(service.state_commits, [])

    def test_split_unsafe_stream_token_falls_back_to_deterministic_result(self):
        csrf = self._login()
        unsafe_response = _tool_response()
        unsafe_response["request_id"] = "https://internal.invalid/trace?token=secret"
        unsafe_response["tool_call"]["arguments"] = {
            "endpoint": "https://private.invalid/api",
            "path": "/srv/media/private",
        }
        unsafe_response["result"]["data"].update({
            "private_url": "https://private.invalid/result",
            "private_path": "/srv/media/private",
            "access_token": "secret-token-value",
        })
        unsafe_response["result"]["evidence"] = [{
            "description": "内部证据 https://private.invalid/evidence",
        }]
        unsafe_response["display"] = {
            "summary": "伪造展示 https://private.invalid/display",
            "details": {"token": "forged-display-secret"},
        }
        service = _StatefulFakeService(unsafe_response)
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
            ["status", "status", "status", "delta", "final"],
        )
        self.assertEqual(events[3]["delta"], "已完成基础检查。")
        final_payload = events[-1]["payload"]
        self.assertEqual(final_payload["request_id"], events[-1]["request_id"])
        self.assertEqual(final_payload["result"]["summary"], "下载队列状态正常")
        self.assertEqual(final_payload["presentation"]["source"], "system")
        self.assertEqual(final_payload["presentation"]["narrative"], "下载队列状态正常")
        self.assertEqual(final_payload["result"]["data"], {"总数": 16})
        self.assertEqual(
            final_payload["tool_call"],
            {"name": "downloads.diagnose_queue", "elapsed_ms": 3},
        )
        self.assertNotIn("arguments", final_payload["tool_call"])
        for private_value in (
            "https://", "private.invalid", "/srv/media/private",
            "secret-token-value", "内部证据", "伪造展示", "forged-display-secret",
            "internal.invalid", "trace?token=secret",
        ):
            self.assertNotIn(private_value, response.text)
        history.assert_called_once()
        saved = history.call_args.kwargs["response"]
        self.assertEqual(saved["result"]["summary"], "下载队列状态正常")
        self.assertEqual(saved["result"]["data"], {"总数": 16})
        self.assertEqual(service.state_commits, ["检查下载队列状态"])

    def test_invalid_final_stream_falls_back_to_deterministic_result(self):
        csrf = self._login()
        service = _FakeService(_tool_response())
        history = Mock()

        async def invalid_final_stream(*_args, **_kwargs):
            yield "内部检查已经完成"

        with (
            patch("app.routes.agent_api.get_agent_service", return_value=service),
            patch("app.routes.agent_api.stream_tool_answer", invalid_final_stream),
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
        self.assertEqual(
            events[-1]["payload"]["result"]["summary"],
            "下载队列状态正常",
        )
        self.assertEqual(
            events[-1]["payload"]["presentation"]["source"],
            "system",
        )
        self.assertEqual(
            events[-1]["payload"]["presentation"]["narrative"],
            "下载队列状态正常",
        )
        self.assertNotIn("内部检查", response.text)
        history.assert_called_once()

    def test_windows_path_stream_token_is_smoothly_redacted(self):
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
            ["status", "status", "status", "delta", "final"],
        )
        self.assertEqual(events[3]["delta"], "已完成基础检查。")
        self.assertIn("[路径已隐藏]", events[-1]["payload"]["presentation"]["narrative"])
        self.assertNotIn("Windows", response.text)
        self.assertNotIn("System32", response.text)
        history.assert_called_once()
        saved = history.call_args.kwargs["response"]
        self.assertIn("[路径已隐藏]", saved["presentation"]["narrative"])

    def test_native_trace_streams_steps_and_reuses_final_card_payload(self):
        csrf = self._login()
        payload = {
            "request_id": "native-stream-request",
            "mode": "conversation",
            "agent_trace": [
                {"label": "核对订阅", "ok": True, "summary": "已完成"},
                {"label": "查询资源站", "ok": False, "summary": "部分超时"},
            ],
            "result": {
                "ok": True,
                "status": "answered",
                "summary": "订阅检查完成",
                "error": "",
                "suggestions": [],
                "data": {},
                "evidence": [],
            },
            "presentation": {
                "version": 1,
                "source": "native",
                "kind": "narrative",
                "narrative": (
                    "订阅、媒体库和资源站已核对完成。\n\n"
                    "- 订阅：正常。\n"
                    "- 媒体库：无需处理。"
                ),
            },
        }
        service = _FakeService(payload)
        history = Mock()

        with (
            patch("app.routes.agent_api.get_agent_service", return_value=service),
            patch("app.routes.agent_api._record_query_history", history),
        ):
            response = self.client.post(
                "/api/agent/query",
                headers={"X-CSRF-Token": csrf},
                json={
                    "message": "检查我的媒体订阅更新",
                    "session_id": SESSION_ID,
                    "stream": True,
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        events = self._events(response)
        self.assertEqual(
            [item["type"] for item in events],
            ["status", "status", "step", "step", "step", "status", "delta", "final"],
        )
        self.assertEqual([item["step"] for item in events[2:5]], ["tool_finish", "tool_finish", "summary"])
        self.assertFalse(events[3]["ok"])
        self.assertEqual(
            events[-1]["payload"]["presentation"]["narrative"],
            "订阅、媒体库和资源站已核对完成。\n\n"
            "- 订阅：正常。\n- 媒体库：无需处理。",
        )
        self.assertEqual(events[-1]["payload"]["presentation"]["source"], "native")
        history.assert_called_once()

    def test_confirmation_response_never_enters_provider_stream(self):
        csrf = self._login()
        response_payload = _tool_response()
        response_payload["mode"] = "confirmation_required"
        response_payload["action_plan"] = {
            "version": 1,
            "plan_id": "confirmation-1",
            "status": "awaiting_approval",
            "title": "执行受控操作",
            "target": "当前对象",
            "impact": "应用预检变更",
            "reversibility": "可手动撤销",
            "risk": "write",
            "preflight_at": "2026-08-31T12:00:00+08:00",
            "decisions": [
                {"id": "execute", "label": "执行"},
                {"id": "cancel", "label": "取消"},
            ],
        }
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
        self.assertEqual(
            events[-1]["payload"]["presentation"],
            {
                "version": 1,
                "source": "system",
                "kind": "narrative",
                "narrative": "下载队列状态正常",
                "degraded": True,
            },
        )

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
        self.assertEqual(service.state_commits, [])


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

    def test_stream_fallback_preserves_only_valid_confirmation_followup(self):
        response = _tool_response()
        response["followup_action"] = {
            "kind": "prepare_confirmation",
            "tool": "guangya.rename.execute",
            "arguments": {},
            "label": "生成执行确认",
        }

        projected = _public_deterministic_fallback_response(
            response, request_id="fallback-followup-request"
        )
        self.assertEqual(projected["followup_action"], response["followup_action"])

        response["followup_action"]["arguments"] = {"unsafe": True}
        rejected = _public_deterministic_fallback_response(
            response, request_id="fallback-followup-rejected"
        )
        self.assertNotIn("followup_action", rejected)

    def test_direct_tool_rejects_incomplete_service_contract(self):
        csrf = self._login()

        class IncompleteService:
            invoke = Mock()

            @staticmethod
            def has_tool(_tool_name: str) -> bool:
                return True

        service = IncompleteService()
        with patch("app.routes.agent_api.get_agent_service", return_value=service):
            response = self.client.post(
                "/api/agent/tools/downloads.diagnose_queue",
                headers={"X-CSRF-Token": csrf},
                json={"session_id": SESSION_ID, "arguments": {}},
            )

        self.assertEqual(response.status_code, 503, response.text)
        self.assertIn("Agent 服务能力暂不可用", response.json()["error"])
        service.invoke.assert_not_called()

    def test_runtime_change_discards_slow_direct_tool_state(self):
        from app.agent.feature_gate import invalidate_agent_runtime_generation

        csrf = self._login()
        headers = {"X-CSRF-Token": csrf}
        service = _BlockingInvokeService(_tool_response())
        direct_client = TestClient(
            create_app(start_background=False), raise_server_exceptions=False
        )
        direct_client.cookies.update(self.client.cookies)

        try:
            with patch("app.routes.agent_api.get_agent_service", return_value=service):
                with ThreadPoolExecutor(max_workers=1) as pool:
                    pending = pool.submit(
                        direct_client.post,
                        "/api/agent/tools/downloads.diagnose_queue",
                        headers=headers,
                        json={"session_id": SESSION_ID, "arguments": {}},
                    )
                    self.assertTrue(service.started.wait(timeout=1))
                    invalidate_agent_runtime_generation()
                    service.release.set()
                    response = pending.result(timeout=2)
        finally:
            service.release.set()
            direct_client.close()

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["code"], "agent_runtime_disabled")
        self.assertEqual(service.state_commits, [])

    def test_slow_direct_tool_cannot_overwrite_newer_query_state(self):
        csrf = self._login()
        headers = {"X-CSRF-Token": csrf}
        service = _BlockingInvokeService(_tool_response())
        direct_client = TestClient(
            create_app(start_background=False), raise_server_exceptions=False
        )
        direct_client.cookies.update(self.client.cookies)

        try:
            with patch("app.routes.agent_api.get_agent_service", return_value=service):
                with ThreadPoolExecutor(max_workers=1) as pool:
                    pending = pool.submit(
                        direct_client.post,
                        "/api/agent/tools/indexer.search_resources",
                        headers=headers,
                        json={
                            "session_id": SESSION_ID,
                            "arguments": {"title": "旧结果"},
                        },
                    )
                    self.assertTrue(
                        service.started.wait(timeout=1),
                        "慢工具未进入执行阶段",
                    )
                    newer = self.client.post(
                        "/api/agent/query",
                        headers=headers,
                        json={
                            "session_id": SESSION_ID,
                            "message": "检查下载队列状态",
                            "request_id": "request_newer_query_0001",
                        },
                    )
                    self.assertEqual(newer.status_code, 200, newer.text)
                    service.release.set()
                    stale = pending.result(timeout=2)
        finally:
            service.release.set()
            direct_client.close()

        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(service.state_commits, [])

    def test_superseded_prepare_discards_unpublished_confirmation(self):
        csrf = self._login()
        headers = {"X-CSRF-Token": csrf}
        service = _HiddenConfirmationService()
        prepare_client = TestClient(
            create_app(start_background=False), raise_server_exceptions=False
        )
        prepare_client.cookies.update(self.client.cookies)

        try:
            with patch("app.routes.agent_api.get_agent_service", return_value=service):
                with ThreadPoolExecutor(max_workers=1) as pool:
                    pending = pool.submit(
                        prepare_client.post,
                        "/api/agent/actions/test.write/prepare",
                        headers=headers,
                        json={"session_id": SESSION_ID, "arguments": {}},
                    )
                    self.assertTrue(service.started.wait(timeout=1))
                    newer = self.client.post(
                        "/api/agent/query",
                        headers=headers,
                        json={
                            "session_id": SESSION_ID,
                            "message": "检查下载队列状态",
                            "request_id": "request_after_hidden_prepare_0001",
                        },
                    )
                    self.assertEqual(newer.status_code, 200, newer.text)
                    service.release.set()
                    stale = pending.result(timeout=2)
        finally:
            service.release.set()
            prepare_client.close()

        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(
            service.confirmation_store.list_active_tickets(owner=service.owner), []
        )

    def test_superseded_query_discards_unpublished_confirmation(self):
        csrf = self._login()
        headers = {"X-CSRF-Token": csrf}
        service = _HiddenConfirmationService()
        old_client = TestClient(
            create_app(start_background=False), raise_server_exceptions=False
        )
        old_client.cookies.update(self.client.cookies)

        try:
            with patch("app.routes.agent_api.get_agent_service", return_value=service):
                with ThreadPoolExecutor(max_workers=1) as pool:
                    pending = pool.submit(
                        old_client.post,
                        "/api/agent/query",
                        headers=headers,
                        json={
                            "session_id": SESSION_ID,
                            "message": "旧确认请求",
                            "request_id": "request_hidden_query_old_0001",
                        },
                    )
                    self.assertTrue(service.started.wait(timeout=1))
                    newer = self.client.post(
                        "/api/agent/query",
                        headers=headers,
                        json={
                            "session_id": SESSION_ID,
                            "message": "检查下载队列状态",
                            "request_id": "request_hidden_query_new_0001",
                        },
                    )
                    self.assertEqual(newer.status_code, 200, newer.text)
                    service.release.set()
                    stale = pending.result(timeout=2)
        finally:
            service.release.set()
            old_client.close()

        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(
            service.confirmation_store.list_active_tickets(owner=service.owner), []
        )

    def test_direct_read_revokes_superseded_query_confirmation_epoch(self):
        csrf = self._login()
        headers = {"X-CSRF-Token": csrf}
        service = _ConfirmationRaceService(_tool_response())
        query_client = TestClient(
            create_app(start_background=False), raise_server_exceptions=False
        )
        query_client.cookies.update(self.client.cookies)

        try:
            with patch("app.routes.agent_api.get_agent_service", return_value=service):
                with ThreadPoolExecutor(max_workers=1) as pool:
                    pending = pool.submit(
                        query_client.post,
                        "/api/agent/query",
                        headers=headers,
                        json={
                            "session_id": SESSION_ID,
                            "message": "执行受控写操作",
                            "request_id": "request_old_confirmation_0001",
                        },
                    )
                    self.assertTrue(
                        service.started.wait(timeout=1),
                        "旧查询未进入确认准备阶段",
                    )
                    direct = self.client.post(
                        "/api/agent/tools/downloads.diagnose_queue",
                        headers=headers,
                        json={"session_id": SESSION_ID, "arguments": {}},
                    )
                    self.assertEqual(direct.status_code, 200, direct.text)
                    service.release.set()
                    stale = pending.result(timeout=2)
        finally:
            service.release.set()
            query_client.close()

        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertTrue(service.owner)
        self.assertEqual(
            service.confirmation_store.list_active_tickets(owner=service.owner),
            [],
        )

    def test_direct_read_preserves_already_published_confirmation(self):
        csrf = self._login()
        headers = {"X-CSRF-Token": csrf}
        service = _ConfirmationRaceService(_tool_response())
        endpoint = "/api/agent/tools/downloads.diagnose_queue"
        payload = {"session_id": SESSION_ID, "arguments": {}}

        with patch("app.routes.agent_api.get_agent_service", return_value=service):
            first = self.client.post(endpoint, headers=headers, json=payload)
            self.assertEqual(first.status_code, 200, first.text)
            generation = service.confirmation_store.owner_generation(
                owner=service.owner
            )
            ticket = service.confirmation_store.issue(
                owner=service.owner,
                tool_name="test.write",
                arguments={"enabled": True},
                expected_owner_generation=generation,
            )

            second = self.client.post(endpoint, headers=headers, json=payload)

        self.assertEqual(second.status_code, 200, second.text)
        active = service.confirmation_store.list_active_tickets(
            owner=service.owner
        )
        self.assertEqual(
            [item.confirmation_id for item in active],
            [ticket.confirmation_id],
        )

    def test_workspace_read_preserves_already_published_confirmation(self):
        csrf = self._login()
        headers = {"X-CSRF-Token": csrf}
        service = _ConfirmationRaceService(_tool_response())
        endpoint = "/api/agent/workspace-actions/invoke"
        payload = {
            "session_id": SESSION_ID,
            "action_key": "review_library_patrol",
        }

        with patch("app.routes.agent_api.get_agent_service", return_value=service):
            first = self.client.post(endpoint, headers=headers, json=payload)
            self.assertEqual(first.status_code, 200, first.text)
            generation = service.confirmation_store.owner_generation(
                owner=service.owner
            )
            ticket = service.confirmation_store.issue(
                owner=service.owner,
                tool_name="test.write",
                arguments={"enabled": True},
                expected_owner_generation=generation,
            )

            second = self.client.post(endpoint, headers=headers, json=payload)

        self.assertEqual(second.status_code, 200, second.text)
        active = service.confirmation_store.list_active_tickets(
            owner=service.owner
        )
        self.assertEqual(
            [item.confirmation_id for item in active],
            [ticket.confirmation_id],
        )

    def test_slow_workspace_action_cannot_overwrite_newer_query_state(self):
        csrf = self._login()
        headers = {"X-CSRF-Token": csrf}
        service = _BlockingWorkspaceService(_tool_response())
        workspace_client = TestClient(
            create_app(start_background=False), raise_server_exceptions=False
        )
        workspace_client.cookies.update(self.client.cookies)

        try:
            with patch("app.routes.agent_api.get_agent_service", return_value=service):
                with ThreadPoolExecutor(max_workers=1) as pool:
                    pending = pool.submit(
                        workspace_client.post,
                        "/api/agent/workspace-actions/invoke",
                        headers=headers,
                        json={
                            "session_id": SESSION_ID,
                            "action_key": "review_library_patrol",
                        },
                    )
                    self.assertTrue(
                        service.started.wait(timeout=1),
                        "慢工作区行动未进入执行阶段",
                    )
                    newer = self.client.post(
                        "/api/agent/query",
                        headers=headers,
                        json={
                            "session_id": SESSION_ID,
                            "message": "检查下载队列状态",
                            "request_id": "request_after_workspace_0001",
                        },
                    )
                    self.assertEqual(newer.status_code, 200, newer.text)
                    service.release.set()
                    stale = pending.result(timeout=2)
        finally:
            service.release.set()
            workspace_client.close()

        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(service.state_commits, [])

    def test_runtime_change_discards_slow_workspace_action_state(self):
        from app.agent.feature_gate import invalidate_agent_runtime_generation

        csrf = self._login()
        headers = {"X-CSRF-Token": csrf}
        service = _BlockingWorkspaceService(_tool_response())
        workspace_client = TestClient(
            create_app(start_background=False), raise_server_exceptions=False
        )
        workspace_client.cookies.update(self.client.cookies)

        try:
            with patch("app.routes.agent_api.get_agent_service", return_value=service):
                with ThreadPoolExecutor(max_workers=1) as pool:
                    pending = pool.submit(
                        workspace_client.post,
                        "/api/agent/workspace-actions/invoke",
                        headers=headers,
                        json={
                            "session_id": SESSION_ID,
                            "action_key": "review_library_patrol",
                        },
                    )
                    self.assertTrue(service.started.wait(timeout=1))
                    invalidate_agent_runtime_generation()
                    service.release.set()
                    response = pending.result(timeout=2)
        finally:
            service.release.set()
            workspace_client.close()

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["code"], "agent_runtime_disabled")
        self.assertEqual(service.state_commits, [])

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
