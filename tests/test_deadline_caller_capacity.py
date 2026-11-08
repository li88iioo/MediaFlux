"""Normal final-answer consumer and memory checkpoint regressions."""
import asyncio

from tests.test_deadline_answer_evidence import EXPLANATION, SUMMARY, AgentInput, AgentSession, InMemorySessionStateStore, KernelToolSpec, ModelEvent, ModelEventType, ModelToolCall, PreparedEffect, ScriptedModel, ToolCatalog, ToolEffect, ToolPipeline, consume_events


def test_normal_submitted_read_answer_preserves_trusted_scope_once():
    async def scenario():
        writes = []
        write = KernelToolSpec(
            name="cloud.submit", domain="cloud", description="提交任务",
            input_schema={"type": "object", "properties": {}}, effect=ToolEffect.WRITE,
            prepare=lambda *_: PreparedEffect(preview={"summary": "预览"}, snapshot_fingerprint="snapshot"),
            execute_confirmed=lambda *_: writes.append(1) or {
                "ok": True, "status": "submitted", "summary": "请求 #276 已提交",
            },
        )
        read = KernelToolSpec(
            name="cloud.inspect", domain="cloud", description="独立目录核验",
            input_schema={"type": "object", "properties": {}}, effect=ToolEffect.READ,
            read=lambda *_: {"ok": True, "summary": SUMMARY,
                            "data": {"scope_note": EXPLANATION, "private_id": "DO-NOT-PUBLISH"}},
        )
        model = ScriptedModel([
            [ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall("submit", write.name, {})), ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")],
            [ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall("inspect", read.name, {})), ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")],
            [ModelEvent(ModelEventType.TEXT_DELTA, text="删除已完成。All writes are finished. DO-NOT-PUBLISH"), ModelEvent(ModelEventType.FINISH, finish_reason="stop")],
        ])
        catalog, store = ToolCatalog([write, read]), InMemorySessionStateStore()
        session = AgentSession(model=model, catalog=catalog,
                               pipeline=ToolPipeline(catalog=catalog, state_store=store), state_store=store)
        preview = await consume_events(session.run(AgentInput(message="提交并检查后续任务", owner="o", session_id="s")))
        assert preview.approval is not None
        final = await consume_events(session.confirm(owner="o", session_id="s", plan_id=preview.approval.plan_id))
        assert writes == [1]
        assert len(model.requests) == 3
        assert "后台任务尚未完成" in final.answer
        assert EXPLANATION in final.answer
        assert final.answer.count(SUMMARY) == 1
        assert final.answer.count("请求 #276 已提交") == 1
        assert "DO-NOT-PUBLISH" not in final.answer
        assert "删除已完成" not in final.answer
        assert "All writes are finished" not in final.answer
        stored = await store.load(owner="o", session_id="s")
        assert stored.conversation[-1]["content"] == final.answer
    asyncio.run(scenario())


def test_memory_unknown_checkpoint_with_supported_no_journal(monkeypatch):
    from tests import test_agent_capacity_recovery as cases
    original = cases.active

    def active(catalog, wire, store):
        session = original(catalog, wire, store)
        # Optional journal is absent, not a different store claiming authority.
        session.journal = None
        return session

    monkeypatch.setattr(cases, "active", active)
    cases.test_cold_explanation_preserves_unknown_across_generations_and_new_read(
        "memory", "chat_completions", True,
    )
