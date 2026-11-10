"""旧工具历史只作事实；入口纠正不能掩盖实际执行失败。"""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from unittest.mock import Mock

import pytest

from app.agent.errors import ToolPipelineError
from app.agent.kernel.adapters import TurnViewBuilder
from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.conversation_context import model_entry_messages
from app.agent.kernel.model import ModelMessage, ModelToolCall
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.session import AgentSession, SessionLimits
from app.agent.kernel.state_types import AgentInput
from app.agent.kernel.tool_calls import PreparedEffect
from app.agent.model_context_budget import bounded_model_messages, conservative_tokens, estimated_tokens
from tests.test_agent_invalid_argument_progress import offline as offline
from tests.test_agent_invalid_argument_progress import store as store
from tests.test_agent_kernel_core import collect, read_tool
from tests.test_agent_recovered_progress import reply
from tests.test_agent_tool_argument_ingress import PROTOCOLS, Wire, encode


def native_calls(protocol, request):
    if protocol == "responses":
        return [row for row in request["input"] if row.get("type") == "function_call"]
    if protocol == "anthropic_messages":
        return [block for row in request["messages"] for block in row["content"]
                if isinstance(block, dict) and block.get("type") == "tool_use"]
    return [call for row in request["messages"] for call in row.get("tool_calls", [])]


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("mixed", [False, True], ids=["separate-native-groups", "mixed-native-group"])
def test_retired_native_groups_become_runtime_data_without_changing_saved_facts(store, protocol, mixed):
    async def run():
        read_hidden = Mock(return_value={
            "ok": True, "status": "success", "summary": "旧对象已读取", "data": {"ref": "old-object"},
        })
        hidden = read_tool("private.inspect", handler=read_hidden)
        workspace = read_tool("agent.run_code")
        catalog = ToolCatalog([hidden, workspace])
        first = reply(protocol, name=hidden.model_name, arguments={}, index=0, private=True)
        if mixed:
            body = json.loads(first[0])
            if protocol == "responses":
                body["output"].append({"type": "function_call", "id": "item-1", "call_id": "call-1",
                                       "name": workspace.model_name, "arguments": "{}"})
            elif protocol == "anthropic_messages":
                body["content"].append({"type": "tool_use", "id": "call-1", "name": workspace.model_name, "input": {}})
            else:
                body["choices"][0]["message"]["tool_calls"].append({"type": "function", "id": "call-1", "function": {
                    "name": workspace.model_name, "arguments": "{}",
                }})
            rounds = [(encode(body).encode(), first[1]), reply(protocol)]
        else:
            rounds = [first, reply(protocol, name=workspace.model_name, arguments={}, index=1, private=True), reply(protocol)]
        pipeline = ToolPipeline(catalog=catalog, state_store=store)
        seed = Wire(protocol, rounds)
        old = AgentSession(model=seed.adapter, catalog=catalog, pipeline=pipeline, state_store=store)
        await collect(old.run(AgentInput(message="读取旧对象", owner="owner-a", session_id="entry-history")))
        before = (await store.load(owner="owner-a", session_id="entry-history")).conversation
        assert before[1]["provider_state"]

        wire = Wire(protocol, [reply(protocol, text="只依据历史事实回答。")])
        session = AgentSession(model=wire.adapter, catalog=catalog, pipeline=pipeline, state_store=store,
                               model_tools=[workspace.name])
        events = await collect(session.run(AgentInput(message="复述此前结果", owner="owner-a", session_id="entry-history")))
        wire.verify()
        request = wire.requests[0]
        calls = native_calls(protocol, request)
        assert len(calls) == (0 if mixed else 1)
        if calls:
            assert workspace.model_name in encode(calls)
        assert hidden.model_name not in encode(calls)
        text = encode(request)
        assert "宿主" in text and "本轮" in text
        assert "旧对象已读取" in text and "old-object" in text
        assert not any(marker + "0" in text for marker in ("PRIVATEOPAQUE", "PRIVATETHINKING", "PRIVATESIGNATURE"))
        if not mixed:
            assert any(marker + "1" in text for marker in ("PRIVATEOPAQUE", "PRIVATETHINKING", "PRIVATESIGNATURE"))
        saved = (await store.load(owner="owner-a", session_id="entry-history")).conversation
        assert saved[:len(before)] == before
        assert events[-1].payload["status"] == "success"
        read_hidden.assert_called_once()
    asyncio.run(run())


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("case", [
    "corrected", "no-correction", "new-rejection", "budget-after-correction", "business-failure", "business-result-failure", "started-entry-error",
])
def test_entry_recovery_preserves_rejections_and_operational_failures(store, protocol, case):
    async def run():
        prepare_hidden, execute_hidden = Mock(), Mock()
        hidden = replace(read_tool("private.change"), effect=ToolEffect.WRITE, read=None,
                         prepare=prepare_hidden, execute_confirmed=execute_hidden)

        def execute(arguments, context):
            if arguments["code"] == "failed-result":
                return {"ok": False, "status": "business_failed", "summary": "真实执行失败"}
            if arguments["code"] == "fail":
                error = ToolPipelineError("真实执行失败", code="tool_not_available" if case == "started-entry-error" else "provider_timeout")
                error.execution_started = True
                raise error
            return {"ok": True, "status": "success", "summary": "合法入口已完成核验"}

        workspace = replace(read_tool("agent.run_code", handler=execute), input_schema={
            "type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"], "additionalProperties": False,
        })
        catalog = ToolCatalog([hidden, workspace])
        steps = [(hidden.model_name, {})]
        if case in {"business-failure", "started-entry-error"}:
            steps.append((workspace.model_name, {"code": "fail"}))
        if case == "business-result-failure":
            steps.append((workspace.model_name, {"code": "failed-result"}))
        if case != "no-correction":
            steps.append((workspace.model_name, {"code": "ok"}))
        if case == "new-rejection":
            steps.append(("removed__inspect", {"ref": "other-object"}))
        limited = case in {"budget-after-correction", "business-failure", "started-entry-error"}
        rounds = [reply(protocol, name=name, arguments=args, index=i, private=True) for i, (name, args) in enumerate(steps)]
        if not limited:
            rounds.append(reply(protocol, text="按当前证据完成核验。"))
        wire = Wire(protocol, rounds)
        session = AgentSession(model=wire.adapter, catalog=catalog, pipeline=ToolPipeline(catalog=catalog, state_store=store),
            state_store=store, model_tools=[workspace.name], limits=SessionLimits(max_model_rounds=len(steps)) if limited else None)
        events = await collect(session.run(AgentInput(message="执行核验", owner="owner-a", session_id="entry-recovery")))
        wire.verify(posts=len(rounds))
        prepare_hidden.assert_not_called()
        execute_hidden.assert_not_called()
        assert not (await store.load(owner="owner-a", session_id="entry-recovery")).pending_effect_plan_id
        final = events[-1].payload
        if case == "no-correction":
            assert events[-1].type.value == "turn.failed" and final["code"] == "tool_not_available"
        else:
            assert final["status"] == ("success" if case == "corrected" else "partial")
        if limited:
            assert final["finish_reason"] == {
                "budget-after-correction": "model_round_budget_exceeded",
                "business-failure": "provider_timeout", "started-entry-error": "tool_not_available",
            }[case]
        if case in {"business-failure", "business-result-failure", "started-entry-error"}:
            assert final["answer"].startswith("部分完成：真实执行失败")
            unfinished = next(line for line in final["answer"].splitlines() if line.startswith("未完成的查询："))
            assert "真实执行失败" in unfinished
            assert "该调用不在宿主提供的模型入口中" not in unfinished
        saved = (await store.load(owner="owner-a", session_id="entry-recovery")).conversation
        assert any(row.get("role") == "tool" and json.loads(row["content"]).get("code") == "tool_not_available" for row in saved)
        for request in wire.requests[1:]:
            assert hidden.model_name not in encode(native_calls(protocol, request))
    asyncio.run(run())


@pytest.mark.parametrize("followup", ["help", "unrelated-query"])
def test_entry_correction_and_terminal_success_never_upgrade_failed_write_receipt(store, followup):
    async def run():
        failure = {"ok": False, "status": "business_failed", "summary": "文件保存失败，目标仍未写入"}
        execute_write = Mock(return_value=failure)
        write = KernelToolSpec(name="fixture.save", domain="fixture", description="保存文件", effect=ToolEffect.WRITE,
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            prepare=lambda *_: PreparedEffect(preview={"summary": "保存预检"}, snapshot_fingerprint="unchanged"),
            execute_confirmed=execute_write)
        command = {"ok": True, "status": "success", "summary": "命令已退出（退出码 0）", "data": {
            "exit_code": 0, "sdk_calls": [] if followup == "help" else [{"method": "library.health", "ok": True, "status": "success"}],
            "stdout": "接口帮助" if followup == "help" else "无关查询成功",
        }}
        read_workspace = Mock(return_value=command)
        workspace = replace(read_tool("agent.run_code", handler=read_workspace), input_schema={
            "type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"], "additionalProperties": False,
        })
        wire = Wire("chat_completions", [
            reply("chat_completions", name=write.model_name, arguments={}, index=0),
            reply("chat_completions", name="retired__save", arguments={}, index=1),
            reply("chat_completions", name=workspace.model_name, arguments={"code": followup}, index=2),
            reply("chat_completions", text="文件保存成功。"),
        ])
        catalog = ToolCatalog([write, workspace])
        session = AgentSession(model=wire.adapter, catalog=catalog, state_store=store,
                               pipeline=ToolPipeline(catalog=catalog, state_store=store))
        preview = await collect(session.run(AgentInput(message="保存文件并核验", owner="owner-a", session_id="entry-write")))
        plan_id = preview[-1].payload["plan_id"]
        session.model_tools = frozenset({workspace.name})
        events = await collect(session.confirm(owner="owner-a", session_id="entry-write", plan_id=plan_id))
        wire.verify(posts=4)
        execute_write.assert_called_once()
        read_workspace.assert_called_once()
        final = events[-1].payload
        assert final["status"] == "partial" and final["finish_reason"] == "business_failed"
        assert "文件保存失败" in final["answer"] and "文件保存成功" not in final["answer"]
        unfinished = next(line for line in final["answer"].splitlines() if line.startswith("未完成的查询："))
        assert "该调用不在宿主提供的模型入口中" not in unfinished
        assert not any(event.type.value == "effect.completed" for event in events)
        view = TurnViewBuilder()
        for event in events:
            view.apply(event)
        assert view.build().effect_result["ok"] is False
        saved = (await store.load(owner="owner-a", session_id="entry-write")).conversation
        receipts = [row for row in saved if row.get("completion_receipt_id") == plan_id]
        assert len(receipts) == 1 and "文件保存失败" in receipts[0]["content"]
    asyncio.run(run())


@pytest.mark.parametrize("conservative", [False, True])
def test_runtime_projection_fits_window_without_discarding_original_audit(conservative):
    messages = [
        ModelMessage(role="user", content="旧请求", history_id="old-user"),
        ModelMessage(role="assistant", tool_calls=(ModelToolCall("old-call", "private__inspect", {}),)),
        ModelMessage(role="tool", tool_name="private__inspect", tool_call_id="old-call",
                     content=encode({"ok": True, "summary": "旧观察", "data": {"value": "x" * 400}})),
        ModelMessage(role="user", content="当前请求", history_id="current-user"),
    ]
    original = [message.to_dict() for message in messages]
    estimate = conservative_tokens if conservative else estimated_tokens
    # 原协议恰好能放下，带宿主事实说明的真实出站视图需要按旧用户回合裁剪。
    budget = sum(8 + estimate(message.to_dict()) for message in messages)
    project = lambda rows: model_entry_messages(rows, available_names={"agent__run_code", "agent.run_code"})
    bounded = bounded_model_messages(messages, history_end=3, tool_definitions=(), system_prompt="", output_tokens=128,
        context_window_tokens=budget + estimate(()) + estimate("") + 128 + 512,
        conservative=conservative, message_projection=project)
    assert sum(8 + estimate(message.to_dict()) for message in project(bounded)) <= budget
    assert bounded[-1] == messages[-1]
    assert [message.to_dict() for message in messages] == original
