"""Native group identity and exact scoped storage-reprojection contracts."""

import asyncio
import json
from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest

from app.agent.errors import ToolPipelineError
from app.agent.kernel import sandbox
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.events import AgentEventType
from app.agent.kernel.model import (
    ModelEvent,
    ModelEventType,
    ModelMessage,
    ModelToolCall,
)
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.projection import ToolOutcome
from app.agent.kernel.provider_model import (
    OpenAICompatibleModelAdapter,
    ProviderSettings,
    UncommittableModelTurn,
)
from app.agent.kernel.session import AgentSession, SessionLimits
from app.agent.kernel.state_types import AgentInput
from app.agent.model_context_budget import (
    archive_native_output,
    bounded_model_messages,
    conservative_tokens,
    estimated_tokens,
)
from app.clients.http import FixedHostHttpClient
from tests.support import isolated_test_database
from tests.test_agent_kernel_core import ScriptedModel, read_tool
from tests.test_agent_provider_state import (
    REDACTED,
    SIGNATURE,
    THINKING,
    WireClient,
    anthropic_answer,
    anthropic_tool_frames,
)
from tests.test_cancelled_read_inline import sqlite_store

# 腾讯真实轨迹 request 6 中的公开 SDK 帮助；不含业务查询结果。
CALENDAR_HELP_OUTPUT = (
    "Help on function calendar in module mf.media:\n\n"
    "calendar(refresh: bool = False) -> dict\n"
    "    返回上海当前周完整days与sources/unscheduled；等待平台排期，不截前20条。"
    "refreshing仅表示平台采集，metadata_pending为可选海报/映射补全，不阻挡使用已发布排期；"
    "partial仍是来源样本而非全站完整。保留VIP/免费事件和来源时间；未知时间不是停更，"
    "旧缓存不是刷新。本周外日期无覆盖；Bangumi周历用media.catalog。\n\n"
)


@pytest.mark.parametrize("protocol", ["responses", "anthropic_messages", "chat_completions"])
def test_native_same_id_distinct_facts_real_wire_and_cold_store(protocol):
    async def run():
        calls = []
        def handler(arguments, context):
            fact = "OLD_FACT" if not calls else "CURRENT_FACT"
            calls.append(fact)
            return {"ok": True, "data": {"fact": fact}}
        catalog = ToolCatalog([read_tool("inspect", handler=handler)])
        reasoning = {"type": "reasoning", "id": "rs_1", "summary": [{"type": "summary_text", "text": THINKING}],
                     "encrypted_content": REDACTED, "status": "completed"}
        if protocol == "responses":
            first = {"status": "completed", "output": [reasoning, {"type": "function_call", "id": "fc_1",
                "call_id": "call-1", "name": "inspect", "arguments": json.dumps({})}]}
            answer = {"status": "completed", "output": [{"type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": "已观察"}]}]}
        elif protocol == "anthropic_messages":
            first, answer = anthropic_tool_frames(), anthropic_answer()
        else:
            first = {"choices": [{"message": {"role": "assistant", "reasoning_content": THINKING,
                "tool_calls": [{"id": "call-1", "type": "function", "function": {
                    "name": "inspect", "arguments": json.dumps({})}}]}, "finish_reason": "tool_calls"}]}
            answer = {"choices": [{"message": {"content": "已观察"}, "finish_reason": "stop"}]}
        wire = WireClient([first, answer, deepcopy(first), answer, answer])
        def session():
            store = sqlite_store()
            return AgentSession(model=OpenAICompatibleModelAdapter(
                ProviderSettings(api_url="https://api.example.com/v1", model="model-one", protocol=protocol),
                client_factory=cast(Callable[..., FixedHostHttpClient], lambda **kwargs: wire)), catalog=catalog,
                pipeline=ToolPipeline(catalog=catalog, state_store=store, reference_store=store), state_store=store,
                system_prompt="inspect")
        original = "最初约束 ORIGINAL_SCOPE_ONLY 禁止下载"
        recent = "补充条件 CURRENT_SCOPE_ONLY 排除HDR"
        conversation = []
        for source_id, request in (("old-source", original), ("recent-source", recent)):
            events = [e async for e in session().run(AgentInput(owner="o", session_id=source_id, message=request))]
            assert events[-1].type == AgentEventType.TURN_COMPLETED
            assert events[-1].payload["status"] == "success"
            saved = await sqlite_store().load(owner="o", session_id=source_id)
            conversation.extend(saved.conversation)
        store = sqlite_store()
        lease, _ = await store.begin_turn(owner="o", session_id="native", request_id="cold-history")
        await store.commit(lease, conversation=conversation)
        assert len(wire.requests) == 4
        cold = await sqlite_store().load(owner="o", session_id="native")
        rows = [r for r in cold.conversation if r["role"] == "tool"]
        assert [json.loads(r["content"])["data"]["fact"] for r in rows] == calls == ["OLD_FACT", "CURRENT_FACT"]
        assert [r["tool_call_id"] for r in rows] == ["call-1", "call-1"]
        native = [r["provider_state"] for r in cold.conversation if r.get("provider_state") and r.get("tool_calls")]
        assert len(native) == 2
        # 新建 store/session，早组条件必须从冷历史进入请求，不能只存在 SQLite。
        events = [e async for e in session().run(AgentInput(owner="o", session_id="native", message="继续核验"))]
        assert events[-1].payload["status"] == "success", events[-1].payload
        request = wire.requests[-1]
        serialized = json.dumps(request, ensure_ascii=False)
        assert original in serialized
        assert recent in serialized
        if protocol == "responses":
            body = request["input"]
            assert reasoning in body
            assert [row["call_id"] for row in body if row.get("type") == "function_call"] == ["call-1"]
            results = [row["output"] for row in body if row.get("type") == "function_call_output"]
        elif protocol == "anthropic_messages":
            blocks = [b for row in request["messages"] if isinstance(row.get("content"), list) for b in row["content"]]
            assert next(b for b in blocks if b.get("type") == "thinking")["signature"] == SIGNATURE
            assert [b["id"] for b in blocks if b.get("type") == "tool_use"] == ["call-1"]
            results = [b["content"] for b in blocks if b.get("type") == "tool_result"]
        else:
            body = request["messages"]
            assert next(row for row in body if row.get("tool_calls"))["reasoning_content"] == THINKING
            assert [call["id"] for row in body for call in row.get("tool_calls", [])] == ["call-1"]
            results = [row["content"] for row in body if row.get("role") == "tool"]
        assert len(results) == 1 and "CURRENT_FACT" in results[0] and "OLD_FACT" not in results[0]
        restored = await sqlite_store().load(owner="o", session_id="native")
        assert [row["provider_state"] for row in restored.conversation if row.get("provider_state") and row.get("tool_calls")] == native
    with isolated_test_database():
        asyncio.run(run())


def terminal_receipt(exit_code: int = 0) -> dict[str, Any]:
    return {"ok": True, "status": "success", "summary": "命令已退出", "data": {
        "exit_code": exit_code, "timed_out": exit_code == 124, "working_directory": "/work",
        "sdk_calls": [{"method": "library.search", "ok": False, "status": "partial",
                       "response_path": "/output/" + "b" * 32 + ".sdk.json",
                       "called_at": "2026-11-05T01:02:03.456789+00:00",
                       "cached": True, "partial": True, "has_more": None}],
        "stdout": "OLD_OUTPUT_" * 1000, "stderr": "error detail\n" * 100,
        "stdout_truncated": False, "stderr_truncated": False,
        "stdout_path": "/output/" + "a" * 32 + ".stdout",
        "stderr_path": "/output/" + "a" * 32 + ".stderr",
    }}


@pytest.mark.parametrize("age", ["current", "recent", "earlier"])
@pytest.mark.parametrize("stdout", [CALENDAR_HELP_OUTPUT, "KNOWN_FACT_" * 1000], ids=["small", "large"])
def test_budget_keeps_readable_facts_inline_when_complete_history_fits(tmp_path, age, stdout):
    receipt = terminal_receipt()
    receipt["data"].update(stdout=stdout, stderr="")
    for stream in ("stdout", "stderr"):
        path = tmp_path / receipt["data"][stream + "_path"].lstrip("/")
        path.parent.mkdir(exist_ok=True)
        path.write_text(receipt["data"][stream])
    messages = [ModelMessage(role="user", content="只读，原始条件不变", history_id="original"),
                ModelMessage(role="assistant", tool_calls=(ModelToolCall("old", "agent__run_code", {"code": "print(facts)"}),)),
                ModelMessage(role="tool", tool_name="agent__run_code", tool_call_id="old", content=json.dumps(receipt))]
    if age == "earlier":
        messages += [ModelMessage(role="user", content="追加条件", history_id="recent"),
                     ModelMessage(role="assistant", content="已记录")]
    history_end = 0 if age == "current" else len(messages)
    if history_end:
        messages.append(ModelMessage(role="user", content="继续核对", history_id="current"))
    messages += [ModelMessage(role="assistant", tool_calls=(ModelToolCall("new", "inspect", {}),)),
                 ModelMessage(role="tool", tool_name="inspect", tool_call_id="new", content='{"ok":true,"data":"UNREAD_FACT"}')]
    view = bounded_model_messages(messages, history_end=history_end, consumed_end=3,
        tool_definitions=(), system_prompt="", context_window_tokens=128000, output_tokens=1024,
        output_available=lambda path: (tmp_path / path.lstrip("/")).is_file())
    results = [message for message in view if message.role == "tool"]
    assert json.loads(results[0].content) == receipt
    assert results[-1] == messages[-1]
    assert [m.content for m in view if m.role == "user"] == [m.content for m in messages if m.role == "user"]


def test_eight_user_turns_keep_all_constraints_and_facts_without_false_history_omissions(tmp_path):
    async def run():
        rounds = []
        for number in range(1, 9):
            rounds += [[ModelEvent(ModelEventType.TOOL_CALL_COMPLETED,
                tool_call=ModelToolCall(str(number), "agent__run_code", {"code": str(number)})),
                ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")],
                [ModelEvent(ModelEventType.TEXT_DELTA, text="已核对"), ModelEvent(ModelEventType.FINISH, finish_reason="stop")]]
        model = ScriptedModel(rounds)
        originals = []
        def read(arguments, context):
            number = int(arguments["code"])
            receipt = terminal_receipt()
            receipt["data"].update(stdout=json.dumps({"fact": number, "dynamic_range": None,
                "dynamic_ranges": [], "stream_verified": False, "evidence": "KNOWN_" * 1000}), stderr="", sdk_calls=[])
            for stream in ("stdout", "stderr"):
                path = "/output/" + f"{number:032x}" + "." + stream
                receipt["data"][stream + "_path"] = path
                location = workspace / path.lstrip("/")
                location.parent.mkdir(exist_ok=True)
                location.write_text(receipt["data"][stream])
            originals.append(deepcopy(receipt))
            return ToolOutcome(model_content=json.dumps(receipt), public_content=receipt)
        paths = SimpleNamespace(data_dir=tmp_path, runtime_dir=tmp_path)
        with patch.object(sandbox, "get_runtime_paths", return_value=paths):
            workspace, _ = sandbox._workspace_location("o", "eight-history")
            workspace.mkdir(parents=True, exist_ok=True)
            store = sqlite_store()
            native = sandbox.run_code_tool(ToolPipeline(catalog=ToolCatalog(), state_store=store))
            catalog = ToolCatalog([replace(native, read=read)])
            session = AgentSession(model=model, catalog=catalog, state_store=store,
                pipeline=ToolPipeline(catalog=catalog, state_store=store, reference_store=store),
                limits=SessionLimits(context_window_tokens=128000, max_output_tokens=1024))
            users, event_ids = [], []
            for number in range(1, 9):
                users.append(f"只读条件 {number}，保留此前条件，不下载")
                events = [e async for e in session.run(AgentInput(owner="o", session_id="eight-history", message=users[-1]))]
                assert events[-1].payload["status"] == "success", events[-1].payload
                before, after = model.requests[-2:]
                for request, expected in ((before, originals[:-1]), (after, originals)):
                    user_rows = [m for m in request.messages if m.role == "user"]
                    assert [m.content for m in user_rows] == users
                    assert [m.history_id for m in user_rows[:-1]] == event_ids
                    assert [json.loads(m.content) for m in request.messages if m.role == "tool"] == expected
                    assert "Historical conversation data" not in request.system_prompt
                event_ids = [m.history_id for m in after.messages if m.role == "user"]
            saved = await sqlite_store().load(owner="o", session_id="eight-history")
            assert [json.loads(r["content"]) for r in saved.conversation if r["role"] == "tool"] == originals
    with isolated_test_database():
        asyncio.run(run())


def test_native_archive_keeps_execution_facts_and_rejects_unreadable_or_effect_results():
    for exit_code in (0, 7, 124):
        original = ModelMessage(role="tool", tool_name="agent__run_code", tool_call_id="call-1",
                                content=json.dumps(terminal_receipt(exit_code)))
        archived = archive_native_output(original, output_available=lambda _: True)
        payload = json.loads(archived.content)
        assert payload["output_archived"] and payload["details_omitted"]
        assert payload["data"] == {key: value for key, value in terminal_receipt(exit_code)["data"].items()
                                   if key not in {"stdout", "stderr"}}
        assert archived.tool_call_id == original.tool_call_id
        assert archive_native_output(original, output_available=lambda _: False) is original
        overrides: tuple[dict[str, Any], ...] = (
            {"tool_name": "library.inspect"}, {"effect_plan_id": "confirmed"},
            {"completion_receipt_id": "receipt"}, {"content": "执行已中断，状态未知"},
        )
        for fields in overrides:
            protected = replace(original, **fields)
            assert archive_native_output(protected, output_available=lambda _: True) is protected
            assert bounded_model_messages([protected], history_end=0, consumed_end=1, tool_definitions=(),
                system_prompt="", context_window_tokens=128000, output_tokens=1024,
                output_available=lambda _: True) == (protected,)


@pytest.mark.parametrize("conservative", [False, True], ids=["estimated", "conservative"])
def test_budget_archives_only_the_oldest_needed_fact_and_keeps_unread_batch(tmp_path, conservative):
    messages = [ModelMessage(role="user", content="只读，保留所有条件", history_id="user")]
    originals = []
    for number, stdout in enumerate((CALENDAR_HELP_OUTPUT, "SECOND_FACT_" * 1000, "UNREAD_FACT_" * 1000), 1):
        receipt = terminal_receipt(7 if number == 1 else 0)
        receipt["data"].update(stdout=stdout, stderr="错误细节" if number == 1 else "")
        for stream in ("stdout", "stderr"):
            path = "/output/" + f"{number:032x}" + "." + stream
            receipt["data"][stream + "_path"] = path
            location = tmp_path / path.lstrip("/")
            location.parent.mkdir(exist_ok=True)
            location.write_text(receipt["data"][stream])
        originals.append(deepcopy(receipt))
        call = ModelToolCall(str(number), "agent__run_code", {"code": "print(facts)"})
        messages += [ModelMessage(role="assistant", tool_calls=(call,)),
                     ModelMessage(role="tool", tool_call_id=call.call_id, tool_name=call.name, content=json.dumps(receipt))]
    estimate = conservative_tokens if conservative else estimated_tokens
    full_cost = sum(8 + estimate(message.to_dict()) for message in messages)
    # 全文仅差一个 token；归档第一条即可放下，不能继续隐去第二条或新批次。
    window = full_cost - 1 + estimate("") + estimate(()) + 1024 + 512
    view = bounded_model_messages(messages, history_end=0, consumed_end=5, tool_definitions=(),
        system_prompt="", context_window_tokens=window, output_tokens=1024, conservative=conservative,
        output_available=lambda path: (tmp_path / path.lstrip("/")).is_file())
    results = [json.loads(message.content) for message in view if message.role == "tool"]
    assert results[0]["output_archived"] and results[0]["details_omitted"]
    assert results[0]["data"] == {key: value for key, value in originals[0]["data"].items() if key not in {"stdout", "stderr"}}
    assert results[1:] == originals[1:]
    assert [m for m in view if m.role != "tool"] == [m for m in messages if m.role != "tool"]
    assert sum(8 + estimate(message.to_dict()) for message in view) <= full_cost - 1
    assert [json.loads(m.content) for m in messages if m.role == "tool"] == originals


@pytest.mark.parametrize("size,externalized", [(0, False), (15, False), (49, True)])
def test_earlier_terminal_requires_strict_serialized_token_saving(size, externalized):
    receipt = terminal_receipt()
    receipt["data"].update(stdout="x" * size, stderr="")
    original = ModelMessage(role="tool", tool_name="agent__run_code", tool_call_id="call-1",
                            content=json.dumps(receipt, ensure_ascii=False, separators=(",", ":")))
    archived = archive_native_output(original, output_available=lambda _: True)
    assert (archived is not original) == externalized
    if externalized:
        assert estimated_tokens(archived.to_dict()) < estimated_tokens(original.to_dict())
    else:
        assert archived is original


@pytest.mark.parametrize("protection", ["none", "stdout", "stderr", "effect", "receipt", "unread"])
def test_budget_pressure_keeps_unread_file_and_write_protections(tmp_path, protection):
    receipt = terminal_receipt()
    receipt["data"].update(stdout=CALENDAR_HELP_OUTPUT, stderr="")
    for stream in ("stdout", "stderr"):
        path = tmp_path / receipt["data"][f"{stream}_path"].lstrip("/")
        path.parent.mkdir(exist_ok=True)
        path.write_text(receipt["data"][stream])
        if protection == stream:
            path.unlink()
    original = ModelMessage(role="tool", tool_name="agent__run_code", tool_call_id="call-1",
                            content=json.dumps(receipt), effect_plan_id="plan" if protection == "effect" else "",
                            completion_receipt_id="receipt" if protection == "receipt" else "")
    messages = [ModelMessage(role="user", content="保留原始条件", history_id="old"),
                ModelMessage(role="assistant", tool_calls=(ModelToolCall("call-1", "agent__run_code", {"code": "help(media.calendar)"}),)),
                original]
    available = lambda path: (tmp_path / path.lstrip("/")).is_file()
    window = sum(8 + estimated_tokens(message.to_dict()) for message in messages) - 1 + 1024 + 512 + estimated_tokens("") + estimated_tokens(())
    def fit():
        return bounded_model_messages(messages, history_end=0, consumed_end=0 if protection == "unread" else 3,
            tool_definitions=(), system_prompt="", context_window_tokens=window, output_tokens=1024, output_available=available)
    if protection == "none":
        view = fit()
        assert json.loads(view[2].content)["output_archived"]
        assert [m.content for m in view if m.role == "user"] == ["保留原始条件"]
    else:
        with pytest.raises(ToolPipelineError) as error:
            fit()
        assert error.value.code == "context_budget_exceeded"
    assert messages[1].tool_calls[0].arguments == {"code": "help(media.calendar)"}
    assert json.loads(original.content) == receipt


@pytest.mark.parametrize("protocol", ["responses", "anthropic_messages", "chat_completions"])
@pytest.mark.parametrize("pressure", [False, True], ids=["ample-budget", "budget-pressure"])
def test_large_historical_output_is_readable_without_retransmitting_on_every_turn(protocol, pressure, tmp_path):
    async def run():
        receipt = terminal_receipt(7)
        receipt["data"]["stdout"] = "OLD_OUTPUT_" * 8000
        stdout_marker = receipt["data"]["stdout"].splitlines()[0]
        arguments = {"code": "print('OLD_OUTPUT_' * 8000)"}
        reasoning = {"type": "reasoning", "id": "rs_1", "summary": [{"type": "summary_text", "text": THINKING}],
                     "encrypted_content": REDACTED, "status": "completed"}
        call = None
        if protocol == "responses":
            call = {"type": "function_call", "id": "fc_1", "call_id": "call-1", "name": "agent__run_code",
                    "arguments": json.dumps(arguments, separators=(",", ":"))}
            first = {"status": "completed", "output": [reasoning, call]}
            answer = {"status": "completed", "output": [{"type": "message", "role": "assistant",
                       "content": [{"type": "output_text", "text": "已观察"}]}]}
        elif protocol == "anthropic_messages":
            first = deepcopy(anthropic_tool_frames())
            for frame in first:
                if frame.get("content_block", {}).get("type") == "tool_use":
                    frame["content_block"]["name"] = "agent__run_code"
                if frame.get("delta", {}).get("type") == "input_json_delta":
                    frame["delta"]["partial_json"] = json.dumps(arguments)
            answer = anthropic_answer()
        else:
            first = {"choices": [{"message": {"role": "assistant", "reasoning_content": THINKING,
                     "tool_calls": [{"id": "call-1", "type": "function", "function": {
                         "name": "agent__run_code", "arguments": json.dumps(arguments)}}]}, "finish_reason": "tool_calls"}]}
            answer = {"choices": [{"message": {"content": "已观察"}, "finish_reason": "stop"}]}
        wire = WireClient([first, answer, answer, answer, answer])
        requests = []
        class CapturingAdapter(OpenAICompatibleModelAdapter):
            async def stream(self, request, *, cancellation):
                requests.append(request)
                async for event in super().stream(request, cancellation=cancellation):
                    yield event
        paths = SimpleNamespace(data_dir=tmp_path, runtime_dir=tmp_path)
        with patch.object(sandbox, "get_runtime_paths", return_value=paths):
            workspace, _ = sandbox._workspace_location("o", "native-output")
            for stream in ("stdout", "stderr"):
                path = workspace / receipt["data"][f"{stream}_path"].lstrip("/")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(receipt["data"][stream])

            def session(window=128000):
                store = sqlite_store()
                catalog = ToolCatalog([sandbox.run_code_tool(ToolPipeline(catalog=ToolCatalog(), state_store=store))])
                native = replace(catalog.get("agent.run_code"), read=lambda *_: ToolOutcome(
                    model_content=json.dumps(receipt), public_content=deepcopy(receipt)))
                catalog = ToolCatalog([native])
                return AgentSession(model=CapturingAdapter(
                    ProviderSettings(api_url="https://api.example.com/v1", model="model-one", protocol=protocol),
                    client_factory=cast(Callable[..., FixedHostHttpClient], lambda **_: wire)),
                    catalog=catalog,  state_store=store,
                    pipeline=ToolPipeline(catalog=catalog, state_store=store, reference_store=store), system_prompt="observe",
                    limits=SessionLimits(context_window_tokens=window, max_output_tokens=1024))

            events = [e async for e in session().run(AgentInput(owner="o", session_id="native-output", message="原始约束一字不丢"))]
            assert events[-1].payload["status"] == "success", events[-1].payload
            saved = await sqlite_store().load(owner="o", session_id="native-output")
            history = [ModelMessage.from_dict(row) for row in saved.conversation]
            next_user = ModelMessage(role="user", content="继续核对", history_id="0" * 16)
            full_cost = sum(8 + estimated_tokens(m.to_dict()) for m in [*history, next_user])
            reserved = estimated_tokens(requests[-1].system_prompt) + estimated_tokens(requests[-1].tools) + 1024 + 512
            window = full_cost + reserved - 1 if pressure else 128000
            for question in ("继续核对", "再次核对"):
                events = [e async for e in session(window).run(AgentInput(owner="o", session_id="native-output", message=question))]
                assert events[-1].payload["status"] == "success", events[-1].payload
            current = wire.requests[1]
            archived = wire.requests[2]
            assert stdout_marker in json.dumps(current, ensure_ascii=False)
            assert stdout_marker not in json.dumps(archived, ensure_ascii=False)
            assert "原始约束一字不丢" in json.dumps(archived, ensure_ascii=False)
            assert "Historical conversation data" not in json.dumps(archived, ensure_ascii=False)
            earlier = wire.requests[3]
            assert stdout_marker not in json.dumps(earlier, ensure_ascii=False)
            assert "原始约束一字不丢" in json.dumps(earlier, ensure_ascii=False)
            assert "继续核对" in json.dumps(earlier, ensure_ascii=False)
            assert "Historical conversation data" not in json.dumps(earlier, ensure_ascii=False)
            body = earlier["input" if protocol == "responses" else "messages"]
            if protocol == "responses":
                assert call is not None
                assert reasoning in body and call in body
                result = next(row for row in body if row.get("type") == "function_call_output")
                assert result["call_id"] == "call-1"
                payload = json.loads(result["output"])
            elif protocol == "anthropic_messages":
                blocks = [block for row in body if isinstance(row.get("content"), list) for block in row["content"]]
                assert next(b for b in blocks if b.get("type") == "thinking")["signature"] == SIGNATURE
                assert next(b for b in blocks if b.get("type") == "redacted_thinking")["data"] == REDACTED
                use = next(b for b in blocks if b.get("type") == "tool_use")
                assert use["id"] == "call-1" and use["input"] == arguments
                result = next(b for b in blocks if b.get("type") == "tool_result")
                assert result["tool_use_id"] == "call-1"
                payload = json.loads(result["content"])
            else:
                assistant = next(row for row in body if row.get("tool_calls"))
                assert assistant["reasoning_content"] == THINKING
                call = assistant["tool_calls"][0]
                assert call["id"] == "call-1" and json.loads(call["function"]["arguments"]) == arguments
                result = next(row for row in body if row.get("role") == "tool")
                assert result["tool_call_id"] == "call-1"
                payload = json.loads(result["content"])
            assert payload["output_archived"] and payload["details_omitted"]
            assert payload["data"]["sdk_calls"] == receipt["data"]["sdk_calls"]
            assert payload["data"]["exit_code"] == receipt["data"]["exit_code"] and not payload["data"]["timed_out"]
            assert "result_handle" not in payload and "read_tool" not in payload
            cold = await sqlite_store().load(owner="o", session_id="native-output")
            assert json.loads(next(r for r in cold.conversation if r["role"] == "tool")["content"])["data"] == receipt["data"]
            current_messages = [ModelMessage.from_dict(row) for row in cold.conversation]
            current_view = bounded_model_messages(current_messages, history_end=0,
                consumed_end=len(current_messages), tool_definitions=(), system_prompt="", context_window_tokens=128000,
                output_tokens=1024, output_available=lambda path: (workspace / path.lstrip("/")).is_file())
            assert [message for message in current_view if message.role == "assistant"] == [message for message in current_messages if message.role == "assistant"]
            current_results = [message for message in current_view if message.role == "tool"]
            assert current_results[0].tool_call_id == "call-1"
            assert not json.loads(current_results[0].content).get("output_archived")
            (workspace / receipt["data"]["stdout_path"].lstrip("/")).unlink()
            events = [e async for e in session().run(AgentInput(owner="o", session_id="native-output", message="文件已清理"))]
            assert events[-1].payload["status"] == "success"
            assert stdout_marker in json.dumps(wire.requests[-1], ensure_ascii=False)
    with isolated_test_database():
        asyncio.run(run())


@pytest.mark.parametrize("parallel,recover", [(False, False), (True, False), (False, True)])
def test_current_terminal_batch_stays_inline_without_budget_pressure_through_recovery(parallel, recover, tmp_path):
    async def run():
        store = sqlite_store()
        paths = SimpleNamespace(data_dir=tmp_path, runtime_dir=tmp_path)
        owner, session_id = "current-output-owner", "current-output-session"
        with patch.object(sandbox, "get_runtime_paths", return_value=paths):
            workspace, _ = sandbox._workspace_location(owner, session_id)
            (workspace / "output").mkdir(parents=True)
            first_batch = set()
            ready = asyncio.Event()
            originals = {}

            async def read(arguments, context):
                name = arguments["code"]
                if name in {"A", "B"}:
                    first_batch.add(name)
                    if len(first_batch) == 2:
                        ready.set()
                    if parallel:
                        await asyncio.wait_for(ready.wait(), 2)
                    stdout = name + "_FACT_" * 1000
                else:
                    stdout = "READBACK:" + "|".join(
                        (workspace / "output" / (letter * 32 + ".stdout")).read_text()[:7]
                        for letter in ("a", "c"))
                receipt = terminal_receipt(7 if name == "A" else 0)
                receipt["data"].update(stdout=stdout, stderr="error detail" if name == "A" else "")
                identity = {"A": "a", "B": "c", "read_full": "d"}[name]
                for stream in ("stdout", "stderr"):
                    path = "/output/" + identity * 32 + "." + stream
                    receipt["data"][stream + "_path"] = path
                    (workspace / path.lstrip("/")).write_text(receipt["data"][stream])
                originals[name] = deepcopy(receipt)
                return ToolOutcome(model_content=json.dumps(receipt), public_content=receipt)

            declaration = sandbox.run_code_tool(ToolPipeline(catalog=ToolCatalog(), state_store=store))
            declaration = replace(declaration, read=read, metadata={**declaration.metadata, "parallel_read": parallel})
            catalog = ToolCatalog([declaration])
            def batch(*names):
                return [*[ModelEvent(ModelEventType.TOOL_CALL_COMPLETED,
                    tool_call=ModelToolCall("call-" + name, "agent__run_code", {"code": name})) for name in names],
                    ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")]
            answer = [ModelEvent(ModelEventType.TEXT_DELTA, text="已核对A/B事实，原始输出可回读。"),
                      ModelEvent(ModelEventType.FINISH, finish_reason="stop")]
            class RecoveryModel(ScriptedModel):
                async def stream(self, request, *, cancellation):
                    if recover and len(self.requests) == 1:
                        self.requests.append(request)
                        yield ModelEvent(ModelEventType.TEXT_DELTA, text="INCOMPLETE_ONLY")
                        raise UncommittableModelTurn("synthetic incomplete stream", reason="incomplete")
                    async for event in super().stream(request, cancellation=cancellation):
                        yield event
            model = RecoveryModel([batch("A", "B"), batch("read_full"), answer, answer])
            def session(state):
                return AgentSession(model=model, catalog=catalog,  state_store=state,
                    pipeline=ToolPipeline(catalog=catalog, state_store=state, reference_store=state))
            events = [event async for event in session(store).run(AgentInput(owner=owner, session_id=session_id,
                message="先核对A/B，保留错误事实，再回读完整结果。"))]
            assert events[-1].payload["status"] == "success"
            seen_request = 2 if recover else 1
            for request in model.requests[1:seen_request + 1]:
                first_seen = [json.loads(message.content) for message in request.messages if message.role == "tool"]
                assert len(first_seen) == 2
                assert [value["data"]["stdout"] for value in first_seen] == [originals[name]["data"]["stdout"] for name in ("A", "B")]
            consumed = [json.loads(message.content) for message in model.requests[seen_request + 1].messages if message.role == "tool"]
            assert len(consumed) == 3
            assert consumed[:2] == [originals[name] for name in ("A", "B")]
            assert consumed[0]["data"]["exit_code"] == 7
            assert consumed[0]["data"]["sdk_calls"] == originals["A"]["data"]["sdk_calls"]
            assert consumed[2]["data"]["stdout"] == originals["read_full"]["data"]["stdout"]
            assert consumed[2]["data"]["stdout"].startswith("READBACK:A_FACT_")
            cold = sqlite_store()
            before = await cold.load(owner=owner, session_id=session_id)
            stored = [json.loads(row["content"]) for row in before.conversation if row["role"] == "tool"]
            assert "INCOMPLETE_ONLY" not in json.dumps(before.conversation)
            assert [value["data"]["stdout"] for value in stored] == [originals[name]["data"]["stdout"] for name in ("A", "B", "read_full")]
            events = [event async for event in session(cold).run(AgentInput(owner=owner, session_id=session_id,
                message="继续按A/B原条件解释，不能把出错说成成功。"))]
            assert events[-1].payload["status"] == "success"
            continued = [json.loads(message.content) for message in model.requests[seen_request + 2].messages if message.role == "tool"]
            assert continued[:2] == [originals[name] for name in ("A", "B")]
            assert continued[2] == originals["read_full"]
            assert continued[0]["data"]["exit_code"] == 7
            assert any("先核对A/B" in message.content for message in model.requests[seen_request + 2].messages)
            after = await cold.load(owner=owner, session_id=session_id)
            assert [row["content"] for row in after.conversation if row["role"] == "tool"] == [row["content"] for row in before.conversation if row["role"] == "tool"]
    with isolated_test_database():
        asyncio.run(run())


@pytest.mark.skipif(not sandbox.sandbox_supported(), reason="真实 Linux root 工作区契约")
def test_current_archive_reads_full_stdout_and_sdk_file_in_real_workspace(tmp_path):
    async def run():
        requests = []
        class WorkspaceModel:
            async def stream(self, request, *, cancellation):
                requests.append(request)
                if len(requests) == 1:
                    code = "from mf import library\nimport json\nprint(json.dumps(library.patrol_status()))\nprint('ROOT_FULL_OUTPUT_' * 400)"
                elif len(requests) == 2:
                    value = json.loads(next(message.content for message in reversed(request.messages) if message.role == "tool"))
                    data = value["data"]
                    code = ("import json\nfrom pathlib import Path\n"
                        + "stdout = Path(" + repr(data["stdout_path"]) + ").read_text()\n"
                        + "sdk = json.loads(Path(" + repr(data["sdk_calls"][0]["response_path"]) + ").read_text())\n"
                        + "print(json.dumps({'stdout_full': 'ROOT_FULL_OUTPUT_' * 400 in stdout, 'sdk_status': sdk['result']['status']}))")
                else:
                    yield ModelEvent(ModelEventType.TEXT_DELTA, text="完整输出与SDK归档已核对。")
                    yield ModelEvent(ModelEventType.FINISH, finish_reason="stop")
                    return
                yield ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall(
                    "root-" + str(len(requests)), "agent__run_code", {"code": code}))
                yield ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")
        with patch.object(sandbox, "get_runtime_paths", return_value=SimpleNamespace(data_dir=tmp_path, runtime_dir=tmp_path)):
            store = sqlite_store()
            catalog = ToolCatalog()
            pipeline = ToolPipeline(catalog=catalog, state_store=store, reference_store=store)
            catalog.register(sandbox.run_code_tool(pipeline))
            session = AgentSession(model=WorkspaceModel(), catalog=catalog,
                state_store=store, pipeline=pipeline)
            owner, sid = "root-current-owner", "root-current-session"
            events = [event async for event in session.run(AgentInput(owner=owner, session_id=sid,
                message="只读检查后台巡检状态，回读完整stdout和SDK响应。"))]
            assert events[-1].payload["status"] == "success"
            second = [json.loads(message.content) for message in requests[1].messages if message.role == "tool"]
            assert "ROOT_FULL_OUTPUT_" in second[0]["data"]["stdout"]
            third = [json.loads(message.content) for message in requests[2].messages if message.role == "tool"]
            assert third[0] == second[0]
            assert json.loads(third[1]["data"]["stdout"]) == {"stdout_full": True, "sdk_status": "not_run"}
            saved = await sqlite_store().load(owner=owner, session_id=sid)
            assert "ROOT_FULL_OUTPUT_" in json.loads(next(row["content"] for row in saved.conversation if row["role"] == "tool"))["data"]["stdout"]
    with isolated_test_database():
        asyncio.run(run())
