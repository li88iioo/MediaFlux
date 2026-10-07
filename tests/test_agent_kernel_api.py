from __future__ import annotations

import asyncio
import json
import types
import unittest
from unittest.mock import AsyncMock, patch

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
        self.activity_calls = []
        self.activity_results = []
        self.cancel_calls = []
        self.cancel_result = True

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

    async def activity(self, *, owner, session_id):
        self.activity_calls.append((owner, session_id))
        if not self.activity_results:
            return None
        result = self.activity_results.pop(0)
        return dict(result) if result is not None else None

    async def cancel(self, *, owner, session_id, request_id=""):
        self.cancel_calls.append((owner, session_id, request_id))
        return self.cancel_result

    async def cancel_effect(self, envelope):
        return True


class FakeStore:
    def __init__(self):
        self.state = types.SimpleNamespace(
            generation=0, conversation=[], pending_effect_plan_id=""
        )
        self.events = []
        self.event_calls = []

    def add_event(self, *, owner, session_id, event):
        self.events.append((owner, session_id, event))

    async def list_sessions(self, *, owner):
        return []

    async def load(self, *, owner, session_id):
        return self.state

    async def list_events(self, *, owner, session_id, limit=200):
        self.event_calls.append((owner, session_id, limit))
        scoped = [
            event for event_owner, event_session, event in self.events
            if event_owner == owner and event_session == session_id
        ]
        return scoped[-limit:]

    async def reset_session(self, *, owner, session_id):
        return types.SimpleNamespace(generation=1)

    async def delete_session(self, *, owner, session_id):
        return True


class FakeEffectStore:
    def __init__(self):
        self.active = True
        self.calls = []
        self.plan = None

    def get_active_plan(self, **scope):
        self.calls.append(dict(scope))
        return self.plan if self.active else None


class FakeApprovalPlan:
    def __init__(self, payload):
        self.payload = payload

    def public_approval_dict(self):
        return dict(self.payload)


class FakeLifecycle:
    def __init__(self):
        self.calls = []
        self.error = None
        self.effect_store = FakeEffectStore()

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

    def test_capabilities_count_matches_the_visible_snapshot(self):
        visible = types.SimpleNamespace(
            name="demo.read", domain="demo", description="visible tool",
            effect=types.SimpleNamespace(value="read"),
        )
        catalog = unittest.mock.Mock()
        catalog.visible.return_value = (visible,)
        catalog.__len__ = unittest.mock.Mock(return_value=3)
        self.runtime.session.catalog = catalog
        response = self.client.get("/api/agent/capabilities")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["count"], len(data["tools"]))
        self.assertEqual([tool["name"] for tool in data["tools"]], ["demo.read"])
        catalog.visible.assert_called_once_with({})

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
        self.assertEqual(self.web.queries[0].request_id, "request-1")

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
        self.assertEqual(self.web.queries[-1].request_id, "")
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

    def test_confirmation_continuation_statuses_are_not_http_conflicts(self):
        for status, expected in (("success", 200), ("partial", 200), ("approval_required", 200), ("failed", 409)):
            with self.subTest(status=status), patch.object(self.web, "confirm_view", new=AsyncMock(return_value=TurnView(
                session_id="session_1234567890", turn_id="confirmed", request_id="confirmed-request", status=status,
            ))):
                response = self.client.post("/api/agent/actions/confirm", json={
                    "plan_id": "plan_1234567890abcdef", "session_id": "session_1234567890", "stream": False,
                })
                self.assertEqual(response.status_code, expected, response.text)
                self.assertEqual(response.json()["status"], status)

    def test_invalid_fields_are_rejected_before_kernel(self):
        response = self.client.post(
            "/api/agent/query",
            json={"message": "x", "session_id": "bad session", "legacy": True},
        )
        self.assertEqual(response.status_code, 400)
        invalid_request_id = self.client.post(
            "/api/agent/query",
            json={
                "message": "x",
                "session_id": "session_1234567890",
                "request_id": "bad/request",
                "stream": False,
            },
        )
        self.assertEqual(invalid_request_id.status_code, 400)
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
        self.lifecycle.effect_store.plan = FakeApprovalPlan({
            "plan_id": "plan-restore-0001",
            "tool_name": "download.pause",
            "effect": "WRITE",
            "preview": {"summary": "暂停任务", "data": {"task": "示例"}},
            "result": {"summary": "预检通过"},
            "confirmation": {
                "action": "暂停下载任务",
                "impact": "任务将停止传输。",
            },
            "expires_at": "2026-09-03T12:05:00+00:00",
        })

        response = self.client.get("/api/agent/sessions/session_1234567890")
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(
            [item["role"] for item in payload["messages"]], ["user", "assistant"]
        )
        self.assertEqual(payload["pending_approval"]["plan_id"], "plan-restore-0001")
        self.assertEqual(payload["pending_approval"]["tool_name"], "download.pause")
        self.assertEqual(payload["pending_approval"]["preview"]["summary"], "暂停任务")
        self.assertEqual(payload["pending_approval"]["result"], {"summary": "预检通过"})
        self.assertEqual(
            payload["pending_approval"]["confirmation"]["action"], "暂停下载任务"
        )
        self.assertEqual(
            self.lifecycle.effect_store.calls,
            [{
                "owner": "webk:v1:" + "a" * 64,
                "session_id": "session_1234567890",
                "generation": 4,
                "plan_id": "plan-restore-0001",
            }],
        )

    def test_session_restore_includes_active_turn_with_safe_phase_detail(self):
        from app.agent.kernel.events import AgentEventType

        owner = "webk:v1:" + "a" * 64
        session_id = "session_1234567890"
        request_id = "request-active-1"
        turn_id = "turn-active-1"
        activity = {
            "request_id": request_id,
            "turn_id": turn_id,
            "generation": 3,
            "protected": False,
            "status": "running",
        }
        self.web.activity_results = [activity, activity]
        self.runtime.store.add_event(
            owner=owner,
            session_id=session_id,
            event={"type": AgentEventType.TURN_STARTED.value, "request_id": request_id, "turn_id": turn_id},
        )
        self.runtime.store.add_event(
            owner=owner,
            session_id=session_id,
            event={
                "type": AgentEventType.TOOL_STARTED.value,
                "request_id": request_id,
                "turn_id": turn_id,
                "payload": {"tool": "private.tool", "arguments": {"token": "private-argument"}},
            },
        )

        response = self.client.get(f"/api/agent/sessions/{session_id}")

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(
            payload["active_turn"],
            {**activity, "detail": "正在执行工具"},
        )
        self.assertIsNone(payload["last_turn"])
        self.assertNotIn("private.tool", response.text)
        self.assertNotIn("private-argument", response.text)
        self.assertEqual(self.web.activity_calls, [(owner, session_id)] * 2)
        self.assertEqual(self.runtime.store.event_calls, [(owner, session_id, 64)])

    def test_session_restore_reloads_persisted_state_when_turn_finishes_during_read(self):
        owner = "webk:v1:" + "a" * 64
        session_id = "session_1234567890"
        request_id = "request-race-1"
        turn_id = "turn-race-1"
        activity = {
            "request_id": request_id,
            "turn_id": turn_id,
            "generation": 7,
            "protected": False,
            "status": "running",
        }
        self.web.activity_results = [activity, None]
        old_state = types.SimpleNamespace(
            generation=7,
            conversation=[{"role": "user", "content": "旧状态"}],
            pending_effect_plan_id="",
        )
        completed_state = types.SimpleNamespace(
            generation=8,
            conversation=[
                {"role": "user", "content": "新问题"},
                {"role": "assistant", "content": "持久化完成结果"},
            ],
            pending_effect_plan_id="",
        )
        self.runtime.store.load = AsyncMock(side_effect=[old_state, completed_state])
        self.runtime.store.add_event(
            owner=owner,
            session_id=session_id,
            event={
                "type": "turn.completed",
                "request_id": request_id,
                "turn_id": turn_id,
                "payload": {"answer": "private event body must not be copied"},
            },
        )

        response = self.client.get(f"/api/agent/sessions/{session_id}")

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["generation"], 8)
        self.assertEqual(payload["messages"][-1]["content"], "持久化完成结果")
        self.assertIsNone(payload["active_turn"])
        self.assertEqual(
            payload["last_turn"],
            {
                "request_id": request_id,
                "turn_id": turn_id,
                "status": "completed",
                "message": "本轮已完成",
            },
        )
        self.assertNotIn("private event body", response.text)
        self.assertEqual(self.runtime.store.load.await_count, 2)
        self.assertEqual(self.runtime.store.event_calls, [(owner, session_id, 64)] * 2)

    def test_session_snapshot_is_uncached_and_draft_scope_matches_list(self):
        history = self.client.get("/api/agent/sessions")
        current = self.client.get("/api/agent/sessions/session_1234567890")
        self.assertEqual(current.status_code, 200)
        self.assertEqual(current.json()["draft_scope"], history.json()["draft_scope"])
        self.assertEqual(current.headers["cache-control"], "private, no-store")

    def test_session_snapshot_counts_only_unresolved_business_effect_waits(self):
        self.runtime.store.state.metadata = {"effect_waits": {
            "plan_pending": {"delivered": False, "receipt_message": ""},
            # 业务结果已有，只剩 Telegram 投递；Web 不需要继续轮询。
            "plan_receipt_ready": {"delivered": False, "receipt_message": "任务已完成"},
            "plan_delivered": {"delivered": True, "receipt_message": ""},
        }}

        response = self.client.get("/api/agent/sessions/session_1234567890")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["pending_effect_count"], 1)

    def test_evicted_turn_start_does_not_hide_an_unconfirmed_terminal_state(self):
        self.runtime.store.add_event(owner="webk:v1:" + "a" * 64, session_id="session_1234567890", event={
            "type": "model.delta", "turn_id": "old-turn", "request_id": "old-request",
            "payload": {"delta": "incomplete"},
        })
        response = self.client.get("/api/agent/sessions/session_1234567890")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["last_turn"]["status"], "interrupted")

    def test_observation_targets_its_request_even_if_old_cancellation_arrives_late(self):
        session_id = "session_1234567890"
        for event in (
            {"type": "turn.completed", "request_id": "current", "turn_id": "current-turn"},
            {"type": "turn.cancelled", "request_id": "previous", "turn_id": "old-turn"},
        ):
            self.runtime.store.add_event(owner="webk:v1:" + "a" * 64, session_id=session_id, event=event)
        response = self.client.get(f"/api/agent/sessions/{session_id}", headers={"X-Agent-Request-Id": "current"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["last_turn"]["request_id"], "current")
        self.assertEqual(response.json()["last_turn"]["status"], "completed")

    def test_fast_completed_turn_between_two_idle_activity_samples_returns_new_result(self):
        session_id = "session_1234567890"
        self.runtime.store.state = types.SimpleNamespace(
            generation=1, conversation=[{"role": "assistant", "content": "旧结果"}], pending_effect_plan_id="",
        )
        async def completed_events(**kwargs):
            self.runtime.store.state = types.SimpleNamespace(
                generation=2, conversation=[{"role": "assistant", "content": "新结果"}], pending_effect_plan_id="",
            )
            return [{"type": "turn.completed", "request_id": "new-request", "turn_id": "new-turn", "payload": {"answer": "新结果"}}]
        self.runtime.store.list_events = AsyncMock(side_effect=completed_events)
        response = self.client.get(f"/api/agent/sessions/{session_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["generation"], 2)
        self.assertEqual(response.json()["messages"][-1]["content"], "新结果")
        self.assertEqual(response.json()["last_turn"]["request_id"], "new-request")

    def test_last_turn_projects_failed_cancelled_and_stale_interrupted_events(self):
        owner = "webk:v1:" + "a" * 64
        session_id = "session_1234567890"
        for event_type, status, message in (
            ("turn.failed", "failed", "本轮未能完成"),
            ("turn.cancelled", "cancelled", "本轮已取消"),
            ("tool.progress", "interrupted", "本轮执行状态未确认，请核对已保存的结果；不会自动重放。"),
        ):
            with self.subTest(event_type=event_type):
                self.runtime.store.events.clear()
                if event_type == "tool.progress":
                    self.runtime.store.add_event(
                        owner=owner,
                        session_id=session_id,
                        event={
                            "type": "turn.started",
                            "request_id": f"request-{event_type}",
                            "turn_id": f"turn-{event_type}",
                        },
                    )
                self.runtime.store.add_event(
                    owner=owner,
                    session_id=session_id,
                    event={
                        "type": event_type,
                        "request_id": f"request-{event_type}",
                        "turn_id": f"turn-{event_type}",
                        "payload": {"message": "untrusted internal detail"},
                    },
                )

                response = self.client.get(f"/api/agent/sessions/{session_id}")

                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(
                    response.json()["last_turn"],
                    {
                        "request_id": f"request-{event_type}",
                        "turn_id": f"turn-{event_type}",
                        "status": status,
                        "message": message,
                    },
                )
                self.assertNotIn("untrusted internal detail", response.text)

    def test_last_turn_events_are_scoped_to_authenticated_owner(self):
        session_id = "session_1234567890"
        self.runtime.store.add_event(
            owner="another-owner",
            session_id=session_id,
            event={
                "type": "turn.failed",
                "request_id": "private-request",
                "turn_id": "private-turn",
            },
        )

        with patch.object(agent_kernel_api, "_owner", return_value="owner-a"):
            response = self.client.get(f"/api/agent/sessions/{session_id}")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()["last_turn"])
        self.assertNotIn("private-request", response.text)
        self.assertEqual(
            self.runtime.store.event_calls,
            [("owner-a", session_id, 64)],
        )

    def test_protected_active_turn_does_not_restore_old_confirmation_card(self):
        session_id = "session_1234567890"
        activity = {
            "request_id": "request-protected-1",
            "turn_id": "turn-protected-1",
            "generation": 4,
            "protected": True,
            "status": "running",
        }
        self.web.activity_results = [activity, activity]
        self.runtime.store.state = types.SimpleNamespace(
            generation=4,
            conversation=[{"role": "user", "content": "执行已确认操作"}],
            pending_effect_plan_id="plan-protected-0001",
        )
        self.lifecycle.effect_store.plan = FakeApprovalPlan({"plan_id": "plan-protected-0001"})

        response = self.client.get(f"/api/agent/sessions/{session_id}")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["active_turn"]["protected"], True)
        self.assertIsNone(response.json()["pending_approval"])
        self.assertEqual(self.lifecycle.effect_store.calls, [])
        self.assertTrue(self.lifecycle.effect_store.active)
        self.assertEqual(self.lifecycle.effect_store.plan.payload["plan_id"], "plan-protected-0001")

    def test_cancel_query_requires_exact_request_id_and_never_wildcard_stops(self):
        owner = "webk:v1:" + "a" * 64
        session_id = "session_1234567890"
        cancelled = self.client.post(
            "/api/agent/query/cancel",
            json={"session_id": session_id, "request_id": "request-old-tab"},
        )
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(cancelled.json()["request_id"], "request-old-tab")
        self.assertEqual(self.web.cancel_calls[-1], (owner, session_id, "request-old-tab"))

        legacy = self.client.post("/api/agent/query/cancel", json={"session_id": session_id})
        self.assertEqual(legacy.status_code, 400, legacy.text)
        self.assertEqual(len(self.web.cancel_calls), 1)

        invalid = self.client.post(
            "/api/agent/query/cancel",
            json={"session_id": session_id, "request_id": "bad/request"},
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(len(self.web.cancel_calls), 1)

    def test_pipeline_snapshots_the_exact_projected_public_result(self):
        from app.agent.confirmation import ConfirmationStore
        from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
        from app.agent.kernel.effects import ConfirmationEffectPlanStore, PreparedEffect
        from app.agent.kernel.pipeline import ToolCallContext, ToolPipeline
        from app.agent.kernel.state import CancellationToken, InMemorySessionStateStore

        state = InMemorySessionStateStore()
        effect_store = ConfirmationEffectPlanStore(ConfirmationStore())
        preview = {"summary": "公开预检摘要"}
        tool = KernelToolSpec(
            name="library.archive",
            domain="library",
            description="归档资源",
            input_schema={"type": "object", "properties": {}},
            effect=ToolEffect.WRITE,
            prepare=lambda _arguments, _context: PreparedEffect(
                preview=preview,
                snapshot_fingerprint="pipeline-result-snapshot",
                metadata={
                    "risk": "write",
                    "confirmation": {"action": "归档资源", "impact": "移动 1 项"},
                },
            ),
            execute_confirmed=lambda *_args: {"summary": "不会在本测试执行"},
        )
        pipeline = ToolPipeline(
            catalog=ToolCatalog([tool]),
            state_store=state,
            effect_store=effect_store,
        )

        async def execute_preview():
            lease, _snapshot = await state.begin_turn(
                owner="owner-pipeline", session_id="session-pipeline", request_id="preview"
            )
            return await pipeline.execute(
                tool.name,
                {},
                context=ToolCallContext(
                    owner="owner-pipeline",
                    session_id="session-pipeline",
                    request_id=lease.request_id,
                    turn_id=lease.turn_id,
                    lease=lease,
                    cancellation=CancellationToken(),
                    report_progress=lambda _payload: asyncio.sleep(0),
                ),
            )

        result = asyncio.run(execute_preview())
        restored = effect_store.get_active_plan(
            owner="owner-pipeline",
            session_id="session-pipeline",
            generation=result.effect_plan.generation,
            plan_id=result.effect_plan.plan_id,
        )
        self.assertEqual(result.outcome.public_content["summary"], "公开预检摘要")
        self.assertNotEqual(result.outcome.public_content, preview)
        self.assertEqual(restored.public_result, result.outcome.public_content)
        self.assertEqual(restored.preview, preview)

    def test_session_restore_uses_persisted_plan_after_approval_event_is_evicted(self):
        from app.agent.confirmation import SQLiteConfirmationStore
        from app.agent.kernel.capabilities import ToolEffect
        from app.agent.kernel.effects import ConfirmationEffectPlanStore, PreparedEffect
        from app.agent.kernel.events import AgentEvent, AgentEventType
        from app.agent.kernel.persistence import SQLiteKernelStore
        from app.agent.kernel.state import StateUpdate
        from tests.support import isolated_test_database

        owner = "webk:v1:" + "a" * 64
        session_id = "session_1234567890"
        preview = {"summary": "冻结预览", "data": {"target": "归档目录"}}
        public_result = {
            "ok": True,
            "status": "preview",
            "summary": "公开预检回执",
            "data": {"target": "归档目录", "checked_items": 3},
            "refs": [],
        }
        confirmation = {"action": "归档文件", "impact": "移动 3 个文件。"}

        with isolated_test_database("agent-api-active-plan.db"):
            with patch("app.modules.web_secret.get_web_secret", return_value="test-confirm-secret"):
                state_store = SQLiteKernelStore(
                    secret_provider=lambda: "test-kernel-secret"
                )
                effect_store = ConfirmationEffectPlanStore(SQLiteConfirmationStore())
                plan = effect_store.freeze(
                    owner=owner,
                    session_id=session_id,
                    generation=4,
                    tool_name="cloud.archive",
                    effect=ToolEffect.WRITE,
                    arguments={"target": "archive"},
                    prepared=PreparedEffect(
                        preview=preview,
                        snapshot_fingerprint="snapshot-4",
                        metadata={"risk": "write", "confirmation": confirmation},
                    ),
                    public_result=public_result,
                )

                async def seed_persisted_state_and_events():
                    lease = None
                    for index in range(4):
                        lease, _ = await state_store.begin_turn(
                            owner=owner,
                            session_id=session_id,
                            request_id=f"generation-{index + 1}",
                        )
                    await state_store.commit(
                        lease,
                        conversation=[
                            {"role": "user", "content": "归档文件"},
                            {"role": "assistant", "content": "确认前检查完成"},
                        ],
                        updates=(StateUpdate("pending_effect_plan_id", plan.plan_id),),
                    )
                    await state_store.append(
                        AgentEvent(
                            type=AgentEventType.EFFECT_APPROVAL_REQUIRED,
                            session_id=session_id,
                            turn_id="approval-turn",
                            request_id="approval-request",
                            sequence=1,
                            payload={
                                "tool": plan.tool_name,
                                "plan": plan.public_dict(),
                                "result": public_result,
                            },
                        ),
                        owner=owner,
                    )
                    for attempt in range(100):
                        for sequence, event_type in enumerate(
                            (AgentEventType.TURN_STARTED, AgentEventType.TURN_FAILED),
                            start=1,
                        ):
                            await state_store.append(
                                AgentEvent(
                                    type=event_type,
                                    session_id=session_id,
                                    turn_id=f"invalid-confirm-{attempt}",
                                    request_id=f"invalid-request-{attempt}",
                                    sequence=sequence,
                                    payload={"code": "confirmation_invalid"},
                                ),
                                owner=owner,
                            )

                asyncio.run(seed_persisted_state_and_events())

                # Rebuild both stores over the same SQLite database, as after a worker restart.
                self.runtime.store = SQLiteKernelStore(
                    secret_provider=lambda: "test-kernel-secret"
                )
                self.lifecycle.effect_store = ConfirmationEffectPlanStore(
                    SQLiteConfirmationStore()
                )
                response = self.client.get(f"/api/agent/sessions/{session_id}")

        self.assertEqual(response.status_code, 200, response.text)
        approval = response.json()["pending_approval"]
        self.assertIsNotNone(approval)
        self.assertEqual(approval["plan_id"], plan.plan_id)
        self.assertEqual(approval["tool_name"], "cloud.archive")
        self.assertEqual(approval["preview"], preview)
        self.assertEqual(approval["result"], public_result)
        self.assertEqual(approval["confirmation"], confirmation)

    def test_sqlite_plan_restore_enforces_scope_expiry_consumption_and_restart(self):
        from app.agent.confirmation import SQLiteConfirmationStore
        from app.agent.kernel.capabilities import ToolEffect
        from app.agent.kernel.effects import ConfirmationEffectPlanStore, PreparedEffect
        from tests.support import isolated_test_database

        owner = "owner-restore"
        session_id = "session_1234567890"
        now = [1_000.0]
        public_result = {"ok": True, "status": "preview", "summary": "原始公开回执"}
        preview = {"summary": "独立冻结预览"}
        confirmation = {"action": "执行操作", "impact": "修改 1 项"}

        with isolated_test_database("agent-plan-scope.db"):
            with patch("app.modules.web_secret.get_web_secret", return_value="test-confirm-secret"):
                legacy_ticket_store = SQLiteConfirmationStore(
                    ttl_seconds=30, clock=lambda: now[0]
                )
                legacy_owner = "owner-v1-restore"
                legacy_session = "legacy_session_123456"
                legacy_ticket = legacy_ticket_store.issue(
                    owner=ConfirmationEffectPlanStore._scoped_owner(
                        legacy_owner, legacy_session
                    ),
                    tool_name="library.archive",
                    arguments={"target": "archive"},
                    context_fingerprint="legacy-snapshot",
                    confirmation_contract={
                        "kernel_effect_version": 1,
                        "session_id": legacy_session,
                        "generation": 3,
                        "effect": ToolEffect.WRITE.value,
                        "preview": preview,
                        "metadata": {"risk": "write"},
                        "audit_contract": {},
                    },
                )
                legacy = ConfirmationEffectPlanStore(
                    SQLiteConfirmationStore(ttl_seconds=30, clock=lambda: now[0])
                ).get_active_plan(
                    owner=legacy_owner,
                    session_id=legacy_session,
                    generation=3,
                    plan_id=legacy_ticket.confirmation_id,
                )
                self.assertIsNotNone(legacy)
                self.assertEqual(legacy.public_result, {})
                self.assertEqual(legacy.public_approval_dict()["confirmation"], {})
                self.assertEqual(
                    set(legacy.public_approval_dict()),
                    {
                        "plan_id", "tool_name", "effect", "preview", "result",
                        "confirmation", "expires_at",
                    },
                )

                first_store = ConfirmationEffectPlanStore(
                    SQLiteConfirmationStore(ttl_seconds=30, clock=lambda: now[0])
                )
                plan = first_store.freeze(
                    owner=owner,
                    session_id=session_id,
                    generation=8,
                    tool_name="library.archive",
                    effect=ToolEffect.WRITE,
                    arguments={"target": "archive"},
                    prepared=PreparedEffect(
                        preview=preview,
                        snapshot_fingerprint="scope-snapshot",
                        metadata={"risk": "write", "confirmation": confirmation},
                    ),
                    public_result=public_result,
                )

                # 新建 store 复用同一 SQLite 文件，模拟另一进程重新读取计划快照。
                store = ConfirmationEffectPlanStore(
                    SQLiteConfirmationStore(ttl_seconds=30, clock=lambda: now[0])
                )
                restored = store.get_active_plan(
                    owner=owner,
                    session_id=session_id,
                    generation=8,
                    plan_id=plan.plan_id,
                )
                self.assertIsNotNone(restored)
                self.assertEqual(restored.public_result, public_result)
                self.assertEqual(restored.preview, preview)
                self.assertEqual(restored.public_approval_dict()["confirmation"], confirmation)
                self.assertIsNone(store.get_active_plan(
                    owner="other-owner",
                    session_id=session_id,
                    generation=8,
                    plan_id=plan.plan_id,
                ))
                self.assertIsNone(store.get_active_plan(
                    owner=owner,
                    session_id="other-session-123456",
                    generation=8,
                    plan_id=plan.plan_id,
                ))
                self.assertIsNone(store.get_active_plan(
                    owner=owner,
                    session_id=session_id,
                    generation=7,
                    plan_id=plan.plan_id,
                ))

                store.claim(
                    owner=owner,
                    session_id=session_id,
                    generation=8,
                    plan_id=plan.plan_id,
                )
                store = ConfirmationEffectPlanStore(
                    SQLiteConfirmationStore(ttl_seconds=30, clock=lambda: now[0])
                )
                self.assertIsNone(store.get_active_plan(
                    owner=owner,
                    session_id=session_id,
                    generation=8,
                    plan_id=plan.plan_id,
                ))

                expiring = store.freeze(
                    owner=owner,
                    session_id=session_id,
                    generation=9,
                    tool_name="library.archive",
                    effect=ToolEffect.WRITE,
                    arguments={"target": "archive"},
                    prepared=PreparedEffect(
                        preview=preview,
                        snapshot_fingerprint="expiring-snapshot",
                        metadata={"risk": "write", "confirmation": confirmation},
                    ),
                    public_result=public_result,
                )
                now[0] = expiring.expires_at + 1
                self.assertIsNone(store.get_active_plan(
                    owner=owner,
                    session_id=session_id,
                    generation=9,
                    plan_id=expiring.plan_id,
                ))

    def test_session_restore_does_not_revive_consumed_or_expired_plan(self):
        self.runtime.store.state = types.SimpleNamespace(
            generation=4, conversation=[], pending_effect_plan_id="plan-stale-0001"
        )
        self.lifecycle.effect_store.active = False

        response = self.client.get("/api/agent/sessions/session_1234567890")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()["pending_approval"])

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
