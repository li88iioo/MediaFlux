"""自然语言模型工具回路→统一预览→持久化人工确认→原解析链的隔离验收。"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import database as db
from app.agent.kernel.adapters import consume_events
from app.agent.kernel.bootstrap import build_agent_kernel_runtime
from app.agent.kernel.capabilities import ToolEffect
from app.agent.kernel.model import ModelEvent, ModelEventType, ModelToolCall
from app.agent.kernel.state import AgentInput
from app.agent.public_safety import public_tool_label
from app.agent.owner_routes import web_kernel_owner
from app.modules.recognition import formats
from app.modules.scraper import _parse_release_core
from tests.support import IsolatedDatabaseTestCase
from tests.test_release_formats import PARENT, TEMPLATE, filename, teaching

PREVIEW = "recognition.preview_release_format"
SAVE = "recognition.save_release_format"
MESSAGE = (
    f"这些文件集号识别错了，教会你这个发布格式以后复用。父目录是{PARENT}。"
    f"{filename(13)} 是星海航行第13集；{filename(14)} 是第14集。r2是修订版。"
    f"另请批量核对{filename(15)}和unrelated.mkv。请先批量预览，再让我确认保存。"
)


class TeachingModel:
    """只替代外部模型推理，工具、数据库、确认及解析器均真实执行。"""

    def __init__(self, *, save=True, arguments=None):
        self.save = save
        self.arguments = arguments or teaching()
        self.requests = []

    async def stream(self, request, *, cancellation):
        cancellation.raise_if_cancelled()
        self.requests.append(request)
        if any("已确认操作的可信系统结果" in message.content for message in request.messages):
            yield ModelEvent(ModelEventType.TEXT_DELTA, text="发布格式已保存，样本已复核。")
            yield ModelEvent(ModelEventType.FINISH, finish_reason="stop")
            return
        step = len(self.requests)
        if step == 1 or (step == 2 and self.save):
            yield ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall(
                f"teaching-{step}", PREVIEW if step == 1 else SAVE, deepcopy(self.arguments),
            ))
            yield ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")
        else:
            yield ModelEvent(ModelEventType.TEXT_DELTA, text="已核对样本中的原始集号，仅预览，尚未保存。")
            yield ModelEvent(ModelEventType.FINISH, finish_reason="stop")


class AgentReleaseFormatKernelTests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM recognition_format_rules")
        formats.invalidate_cache()
        self.owner = web_kernel_owner("teaching-test")
        self.other_owner = web_kernel_owner("other-user")

    def runtime(self, model):
        return build_agent_kernel_runtime(model=model)

    def test_natural_request_runs_read_then_one_confirmation_and_reuses_core(self):
        async def scenario():
            model = TeachingModel()
            runtime = self.runtime(model)
            view = await consume_events(runtime.session.run(AgentInput(
                message=MESSAGE, owner=self.owner, session_id="teaching-session",
            )))
            self.assertEqual(view.status, "approval_required", view.to_dict())
            self.assertEqual(formats.list_rules(), [])
            self.assertEqual(len(model.requests), 2)
            instruction = json.dumps(model.requests[0].tools, ensure_ascii=False)
            self.assertIn("不要要求用户写模板", instruction)
            self.assertIn("缺少信息先询问", instruction)
            self.assertTrue(any(message.role == "tool" for message in model.requests[1].messages))
            approval = view.approval.to_dict()
            self.assertEqual(approval["tool_name"], SAVE)
            self.assertIn("保存发布格式", approval["confirmation"]["action"])
            public = json.dumps(approval, ensure_ascii=False)
            for private in ("preview_token", TEMPLATE, PARENT, "snapshot_fingerprint"):
                self.assertNotIn(private, public)
            self.assertIn("13", public)
            self.assertIn("星海航行", public)
            confirmed = await consume_events(runtime.session.confirm(
                owner=self.owner, session_id="teaching-session", plan_id=approval["plan_id"],
            ))
            self.assertEqual(confirmed.status, "success", confirmed.to_dict())
            self.assertIn("已保存", confirmed.answer)
            self.assertTrue(confirmed.effect_result["ok"], confirmed.to_dict())
            self.assertEqual(len(formats.list_rules()), 1)
            self.assertEqual(_parse_release_core(filename(15), PARENT).context.episode, 15)
            duplicate = await consume_events(runtime.session.confirm(
                owner=self.owner, session_id="teaching-session", plan_id=approval["plan_id"],
            ))
            self.assertNotEqual(duplicate.status, "approval_required")
            self.assertEqual(len(formats.list_rules()), 1)
        asyncio.run(scenario())

    def test_read_only_request_never_prepares_a_write(self):
        async def scenario():
            runtime = self.runtime(TeachingModel(save=False))
            view = await consume_events(runtime.session.run(AgentInput(
                message=MESSAGE.replace("先批量预览，再让我确认保存", "仅批量预览，不要保存"),
                owner=self.owner, session_id="preview-session",
            )))
            self.assertEqual(view.status, "success", view.to_dict())
            self.assertIsNone(view.approval)
            state = await runtime.store.load(owner=self.owner, session_id="preview-session")
            self.assertFalse(state.pending_effect_plan_id)
            self.assertEqual(formats.list_rules(), [])
        asyncio.run(scenario())

    def test_owner_boundary_and_cancel_leave_rule_store_unchanged(self):
        async def scenario():
            runtime = self.runtime(TeachingModel())
            view = await consume_events(runtime.session.run(AgentInput(
                message=MESSAGE, owner=self.owner, session_id="private-session",
            )))
            self.assertIsNotNone(view.approval, view.to_dict())
            plan_id = view.approval.plan_id
            denied = await consume_events(runtime.session.confirm(
                owner=self.other_owner, session_id="private-session", plan_id=plan_id,
            ))
            self.assertEqual(denied.status, "failed", denied.to_dict())
            self.assertEqual(formats.list_rules(), [])
            self.assertTrue(await runtime.session.cancel_effect(
                owner=self.owner, session_id="private-session", plan_id=plan_id,
            ))
            cancelled = await consume_events(runtime.session.confirm(
                owner=self.owner, session_id="private-session", plan_id=plan_id,
            ))
            self.assertEqual(cancelled.status, "failed")
            self.assertEqual(formats.list_rules(), [])
        asyncio.run(scenario())

    def test_pending_plan_can_be_reloaded_but_process_restart_invalidates_preview(self):
        async def scenario():
            runtime = self.runtime(TeachingModel())
            view = await consume_events(runtime.session.run(AgentInput(
                message=MESSAGE, owner=self.owner, session_id="reload-session",
            )))
            self.assertIsNotNone(view.approval, view.to_dict())
            restored = self.runtime(TeachingModel())
            state = await restored.store.load(owner=self.owner, session_id="reload-session")
            self.assertEqual(state.pending_effect_plan_id, view.approval.plan_id)
            with patch.object(formats, "_PREVIEW_EPOCH", "a-different-process"):
                result = await consume_events(restored.session.confirm(
                    owner=self.owner, session_id="reload-session", plan_id=view.approval.plan_id,
                ))
            self.assertEqual(result.status, "approval_required", result.to_dict())
            self.assertIsNotNone(result.approval)
            self.assertNotEqual(result.approval.plan_id, view.approval.plan_id)
            self.assertEqual(result.effect_result["status"], "confirmation_stale")
            self.assertEqual(formats.list_rules(), [])
            replay = await consume_events(restored.session.confirm(
                owner=self.owner, session_id="reload-session", plan_id=view.approval.plan_id,
            ))
            self.assertEqual(replay.status, "failed")
            state = await restored.store.load(owner=self.owner, session_id="reload-session")
            self.assertEqual(state.pending_effect_plan_id, result.approval.plan_id)
            self.assertEqual(formats.list_rules(), [])
        asyncio.run(scenario())

    def test_reloaded_confirmation_executes_frozen_plan_before_model_continuation(self):
        async def scenario():
            runtime = self.runtime(TeachingModel())
            view = await consume_events(runtime.session.run(AgentInput(
                message=MESSAGE, owner=self.owner, session_id="stored-confirm-session",
            )))
            self.assertIsNotNone(view.approval, view.to_dict())
            model = TeachingModel()
            restored = self.runtime(model)
            result = await consume_events(restored.session.confirm(
                owner=self.owner, session_id="stored-confirm-session", plan_id=view.approval.plan_id,
            ))
            self.assertEqual(result.status, "success", result.to_dict())
            self.assertEqual(len(model.requests), 1)
            self.assertTrue(any("可信系统结果" in message.content for message in model.requests[0].messages))
            self.assertEqual(len(formats.list_rules()), 1)
        asyncio.run(scenario())

    def test_model_cannot_bypass_confirmation_or_invent_missing_samples(self):
        async def scenario():
            invalid = (dict(teaching(), confirmed=True), {**teaching(), "examples": teaching()["examples"][:1]})
            for index, arguments in enumerate(invalid):
                runtime = self.runtime(TeachingModel(arguments=arguments))
                view = await consume_events(runtime.session.run(AgentInput(
                    message=MESSAGE, owner=self.owner, session_id=f"invalid-teaching-{index}",
                )))
                self.assertIsNone(view.approval, view.to_dict())
                self.assertEqual(formats.list_rules(), [])
                state = await runtime.store.load(owner=self.owner, session_id=f"invalid-teaching-{index}")
                self.assertFalse(state.pending_effect_plan_id)
        asyncio.run(scenario())

    def test_tools_have_one_shared_request_contract_and_mandatory_effect_gate(self):
        runtime = self.runtime(TeachingModel())
        preview, save = (runtime.session.catalog.get(name) for name in (PREVIEW, SAVE))
        self.assertEqual(preview.effect, ToolEffect.READ)
        self.assertEqual(save.effect, ToolEffect.WRITE)
        self.assertEqual(preview.input_schema, save.input_schema)
        for tool in (preview, save):
            self.assertNotIn("preview_token", tool.input_schema["properties"])
            self.assertNotIn("confirmed", tool.input_schema["properties"])
            self.assertIn("发布格式", public_tool_label(tool.name))
        self.assertEqual(preview.validator(teaching()), formats.normalize_request(teaching()))
        self.assertFalse(hasattr(formats, "_request"))

    def test_web_chat_and_confirmation_use_real_persistent_runtime(self):
        from tests.test_release_formats import ReleaseFormatApiTests
        from app.main import create_app
        runtime = self.runtime(TeachingModel())
        with patch("app.routes.agent_api.get_agent_kernel_runtime", return_value=runtime):
            client = TestClient(create_app(), raise_server_exceptions=False)
            self.addCleanup(client.close)
            login = client.get("/login")
            response = client.post("/login", data={
                "csrf_token": ReleaseFormatApiTests.csrf(login), "username": "admin", "password": "123456",
            }, follow_redirects=False)
            self.assertEqual(response.status_code, 302)
            headers = {"X-CSRF-Token": ReleaseFormatApiTests.csrf(client.get("/agent"))}
            response = client.post("/api/agent/query", json={
                "message": MESSAGE, "session_id": "teaching-http-session", "stream": False,
            }, headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            payload = response.json()
            self.assertEqual(payload["status"], "approval_required", payload)
            self.assertEqual(formats.list_rules(), [])
            response = client.post("/api/agent/actions/confirm", json={
                "session_id": "teaching-http-session", "plan_id": payload["approval"]["plan_id"], "stream": False,
            }, headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["status"], "success", response.text)
            self.assertTrue(response.json()["effect_result"]["ok"])
            self.assertEqual(len(client.get("/api/tools/release-formats").json()["items"]), 1)
            self.assertEqual(_parse_release_core(filename(15), PARENT).context.episode, 15)
