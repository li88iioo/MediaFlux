from __future__ import annotations

import json
import types
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent.kernel.adapters import TurnView
from app.agent.kernel.state import SessionBusyError
from app.routes import agent_api as agent_kernel_api


class FakeCatalog:
    def visible(self, _context):
        return ()

    def __len__(self):
        return 0


class FakeWeb:
    def __init__(self):
        self.queries = []
        self.confirmations = []

    async def query(self, envelope):
        self.queries.append(envelope)
        for item in (
            {
                "event_id": "one",
                "type": "turn.started",
                "occurred_at": "2026-09-03T00:00:00Z",
                "sequence": 1,
                "session_id": envelope.session_id,
                "turn_id": "turn-one",
                "request_id": envelope.request_id,
                "payload": {"channel": "web"},
            },
            {
                "event_id": "two",
                "type": "turn.completed",
                "occurred_at": "2026-09-03T00:00:01Z",
                "sequence": 2,
                "session_id": envelope.session_id,
                "turn_id": "turn-one",
                "request_id": envelope.request_id,
                "payload": {"status": "success", "answer": "完成"},
            },
        ):
            yield (json.dumps(item, ensure_ascii=False) + "\n").encode()

    async def query_view(self, envelope):
        self.queries.append(envelope)
        return TurnView(
            session_id=envelope.session_id,
            turn_id="turn-one",
            request_id=envelope.request_id,
            status="success",
            answer="完成",
        )

    async def confirm_view(self, envelope):
        self.confirmations.append(envelope)
        return TurnView(
            session_id=envelope.session_id,
            turn_id="turn-confirm",
            request_id=envelope.request_id,
            status="effect_completed",
            effect_result={"summary": "已执行"},
        )

    async def confirm(self, envelope):
        self.confirmations.append(envelope)
        yield b'{"type":"effect.completed"}\n'

    async def cancel(self, *, owner, session_id):
        return True

    async def cancel_effect(self, envelope):
        return True


class FakeStore:
    def __init__(self):
        self.state = types.SimpleNamespace(
            generation=0, conversation=[], pending_effect_plan_id=""
        )
        self.events = []

    async def list_sessions(self, *, owner):
        return []

    async def load(self, *, owner, session_id):
        return self.state

    async def list_events(self, *, owner, session_id, limit=200):
        return list(self.events)[-limit:]

    async def reset_session(self, *, owner, session_id):
        return types.SimpleNamespace(generation=1)

    async def delete_session(self, *, owner, session_id):
        return True


class FakeLifecycle:
    def __init__(self):
        self.calls = []
        self.error = None

    async def reset(self, *, owner, session_id):
        self.calls.append(("reset", owner, session_id))
        if self.error is not None:
            raise self.error
        return types.SimpleNamespace(generation=1)

    async def delete(self, *, owner, session_id):
        self.calls.append(("delete", owner, session_id))
        if self.error is not None:
            raise self.error
        return True


class AgentKernelApiTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(agent_kernel_api.router)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.web = FakeWeb()
        self.lifecycle = FakeLifecycle()
        self.runtime = types.SimpleNamespace(
            web=self.web,
            session=types.SimpleNamespace(catalog=FakeCatalog()),
            store=FakeStore(),
            lifecycle=self.lifecycle,
            metrics=types.SimpleNamespace(snapshot=lambda: {"turns": 0}),
        )
        self.patches = [
            patch.object(agent_kernel_api, "require_api_login", return_value=None),
            patch.object(agent_kernel_api, "_require_enabled", return_value=None),
            patch.object(
                agent_kernel_api, "_owner", return_value="webk:v1:" + "a" * 64
            ),
            patch.object(
                agent_kernel_api, "get_agent_kernel_runtime", return_value=self.runtime
            ),
            patch.object(
                agent_kernel_api.agent_rate_limiter, "allow", return_value=True
            ),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.client.close()

    def test_query_streams_canonical_ndjson_without_trace_replay_wrapper(self):
        response = self.client.post(
            "/api/agent/query",
            json={
                "message": "检查媒体库",
                "session_id": "session_1234567890",
                "request_id": "request-1",
                "stream": True,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(
            response.headers["content-type"].startswith("application/x-ndjson")
        )
        events = [json.loads(line) for line in response.text.splitlines()]
        self.assertEqual(
            [item["type"] for item in events], ["turn.started", "turn.completed"]
        )
        self.assertEqual(events[-1]["payload"]["answer"], "完成")

    def test_non_stream_query_and_confirm_return_canonical_turn_view(self):
        query = self.client.post(
            "/api/agent/query",
            json={
                "message": "检查媒体库",
                "session_id": "session_1234567890",
                "stream": False,
            },
        )
        self.assertEqual(query.status_code, 200, query.text)
        self.assertEqual(query.json()["answer"], "完成")
        confirmed = self.client.post(
            "/api/agent/actions/confirm",
            json={
                "plan_id": "plan_1234567890abcdef",
                "session_id": "session_1234567890",
            },
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertEqual(confirmed.json()["status"], "effect_completed")
        self.assertEqual(len(self.web.confirmations), 1)

    def test_invalid_fields_are_rejected_before_kernel(self):
        response = self.client.post(
            "/api/agent/query",
            json={"message": "x", "session_id": "bad session", "legacy": True},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.web.queries, [])

    def test_session_restore_reconstructs_matching_pending_approval(self):
        self.runtime.store.state = types.SimpleNamespace(
            generation=4,
            conversation=[
                {"role": "user", "content": "暂停下载任务"},
                {"role": "assistant", "content": "已生成计划"},
                {"role": "tool", "content": "内部结果不应展示"},
            ],
            pending_effect_plan_id="plan-restore-0001",
        )
        self.runtime.store.events = [
            {
                "type": "effect.approval_required",
                "payload": {
                    "tool": "download.pause",
                    "plan": {
                        "plan_id": "plan-restore-0001",
                        "tool_name": "download.pause",
                        "effect": "WRITE",
                        "preview": {"summary": "暂停任务", "data": {"task": "示例"}},
                        "confirmation": {
                            "action": "暂停下载任务",
                            "impact": "任务将停止传输。",
                        },
                        "expires_at": "2026-09-03T12:05:00+00:00",
                    },
                    "result": {"summary": "预检通过"},
                },
            }
        ]

        response = self.client.get("/api/agent/sessions/session_1234567890")
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(
            [item["role"] for item in payload["messages"]], ["user", "assistant"]
        )
        self.assertEqual(payload["pending_approval"]["plan_id"], "plan-restore-0001")
        self.assertEqual(payload["pending_approval"]["tool_name"], "download.pause")
        self.assertEqual(payload["pending_approval"]["preview"]["summary"], "暂停任务")
        self.assertEqual(
            payload["pending_approval"]["confirmation"]["action"], "暂停下载任务"
        )

    def test_session_restore_filters_intermediate_tool_turns_and_uses_public_result(self):
        self.runtime.store.state = types.SimpleNamespace(
            generation=5,
            conversation=[
                {"role": "user", "content": "搜索并推送 4K 版"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"call_id": "call-1", "name": "indexer.search_resources"}
                    ],
                },
                {
                    "role": "tool",
                    "content": "内部工具结果",
                    "tool_name": "indexer.search_resources",
                },
                {
                    "role": "assistant",
                    "content": "已确认操作的可信系统结果（不是待执行计划）：\n{\"request_id\":54}",
                    "tool_name": "ingest.submit",
                    "public_content": "⚠️ 批量提交完成：2 个已受理，1 个未受理",
                },
            ],
            pending_effect_plan_id="",
        )

        response = self.client.get("/api/agent/sessions/session_1234567890")

        self.assertEqual(response.status_code, 200, response.text)
        messages = response.json()["messages"]
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[-1]["content"], "⚠️ 批量提交完成：2 个已受理，1 个未受理")
        self.assertEqual(
            messages[-1]["tools"],
            ["indexer.search_resources", "ingest.submit"],
        )
        self.assertNotIn("request_id", response.text)
        self.assertNotIn("查询已完成", response.text)

    def test_query_rate_limit_returns_429(self):
        with patch.object(
            agent_kernel_api.agent_rate_limiter, "allow", return_value=False
        ):
            response = self.client.post(
                "/api/agent/query",
                json={"message": "检查媒体库", "session_id": "session_1234567890"},
            )
        self.assertEqual(response.status_code, 429)

    def test_reset_and_delete_use_unified_lifecycle(self):
        reset = self.client.post(
            "/api/agent/session/reset",
            json={"session_id": "session_1234567890"},
        )
        deleted = self.client.delete(
            "/api/agent/sessions/session_1234567890"
        )

        self.assertEqual(reset.status_code, 200, reset.text)
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(
            [call[0] for call in self.lifecycle.calls],
            ["reset", "delete"],
        )

    def test_session_lifecycle_rejects_protected_effect_with_409(self):
        self.lifecycle.error = SessionBusyError("confirmed effect is executing")

        response = self.client.post(
            "/api/agent/session/reset",
            json={"session_id": "session_1234567890"},
        )

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["code"], "effect_in_progress")


if __name__ == "__main__":
    unittest.main()
