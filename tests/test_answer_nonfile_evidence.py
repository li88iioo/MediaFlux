"""ID32: native URL 回执与同请求读证据经过 Session，后端完全离线。"""
import asyncio
from unittest.mock import patch

import pytest

from app import sdk
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.ports.sdk_effects import download_submit_tool
from app.agent.public_view import public_conversation_messages
from tests.support import isolated_test_database
from tests.test_agent_kernel_core import AgentInput, AgentSession, KernelToolSpec, ModelEvent, ModelEventType, ModelToolCall, ScriptedModel, ToolCatalog, ToolEffect, ToolPipeline, ToolPipelineError, consume_events

EXPLANATION = "提交后，请求#42的本地记录显示qB正在下载，整理与STRM仍待处理，因此尚不能确认入库完成。"


def tool_round(*calls):
    return [*[ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=c) for c in calls],
            ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")]


@pytest.mark.parametrize("unresolved,foreign", [(False, False), (True, False), (False, True)])
def test_native_url_followup_explanation(unresolved, foreign):
    async def scenario():
        args = {"url": "magnet:?xt=urn:btih:" + "a" * 40, "target": "qb"}
        row = {"id": 43 if foreign else 42, "title": "Offline fixture", "kind": "magnet",
               "targets": "qb", "status": "downloading", "qb_status": "downloading",
               "gy_status": "not_started", "organize_status": "pending", "strm_status": "pending",
               "updated_at": "2026-01-01 00:00:00", "error": "", "source_value": ""}
        reads = []
        def inspect(*_):
            with patch("app.repositories.activity.snapshot", return_value={
                "record": row, "logs": [], "local_tasks": [], "local_task_status_counts": {},
                "verification": None, "runs": [],
            }):
                result = sdk.activity.timeline("download", row["id"])
            reads.append(result)
            return result
        def blocked(*_):
            raise ToolPipelineError("另一个请求不可读", code="inspection_blocked")
        schema = {"type": "object", "properties": {}}
        write = download_submit_tool()
        read = KernelToolSpec(name="activity.timeline", domain="activity", description="核验进度", input_schema=schema,
                              effect=ToolEffect.READ, read=inspect)
        bad = KernelToolSpec(name="activity.blocked", domain="activity", description="其它核验", input_schema=schema,
                             effect=ToolEffect.READ, read=blocked)
        calls = [ModelToolCall("inspect", read.name, {})]
        if unresolved:
            calls.append(ModelToolCall("blocked", bad.name, {}))
        cancellations = []
        class EvidenceModel(ScriptedModel):
            async def stream(self, request, *, cancellation):
                cancellations.append(cancellation)
                if len(self.requests) == 2:
                    wire = "\n".join(m.content for m in request.messages if m.role == "tool")
                    assert '"downloading"' in wire and '光鸭整理' in wire
                    assert EXPLANATION not in wire
                async for event in super().stream(request, cancellation=cancellation):
                    yield event
        model = EvidenceModel([
            tool_round(ModelToolCall("submit", write.name, args)), tool_round(*calls),
            [ModelEvent(ModelEventType.TEXT_DELTA, text=EXPLANATION + "已全部入库完成。允许自动重放。"),
             ModelEvent(ModelEventType.FINISH, finish_reason="stop")],
        ])
        catalog = ToolCatalog([write, read, bad])
        store = SQLiteKernelStore(secret_provider=lambda: "a32-offline-fixture")
        session = AgentSession(model=model, catalog=catalog,
                               pipeline=ToolPipeline(catalog=catalog, state_store=store), state_store=store)
        with patch("app.indexers.downloads.download_target_readiness", return_value={"qb": True}), \
             patch("app.indexers.downloads.create_request", return_value={"id": 42, "created": True, "status": "pending"}) as create, \
             patch("app.indexers.downloads.dispatch_request", return_value={"ok": True, "status": "submitted", "succeeded": ["qb"], "failed": []}) as dispatch:
            preview = await consume_events(session.run(AgentInput(message="提交下载并核验进度", owner="o", session_id="s")))
            assert preview.approval is not None
            final = await consume_events(session.confirm(owner="o", session_id="s", plan_id=preview.approval.plan_id))
            create.assert_called_once()
            dispatch.assert_called_once_with(
                42, "qb", check_cancelled=cancellations[-1].raise_if_cancelled
            )
        assert len(reads) == 1 and len(model.requests) == 3
        cold = await SQLiteKernelStore(secret_provider=lambda: "a32-offline-fixture").load(owner="o", session_id="s")
        assert cold.conversation[-1]["content"] == final.answer
        assert public_conversation_messages(cold.conversation)[-1]["content"] == final.answer
        assert "后台任务尚未完成" in final.answer
        assert "已全部入库完成" not in final.answer and "允许自动重放" not in final.answer
        if unresolved:
            assert final.status == "partial"
            assert "另一个请求不可读" in final.answer
            assert "新的写操作仍需确认，不会自动重放" in final.answer
        if foreign:
            assert EXPLANATION not in final.answer
        else:
            assert EXPLANATION in final.answer
    with isolated_test_database("a32-nonfile.db"):
        asyncio.run(scenario())
