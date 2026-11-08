"""对象恢复控制部分状态与中断兜底；正常答案由模型依据原始观察生成。"""
from __future__ import annotations

import asyncio
import json
import socket
from typing import Any

import pytest

from app import database as db
from app.agent.errors import ToolPipelineError
from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.tool_calls import PreparedEffect
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.session import AgentSession, SessionLimits
from app.agent.kernel.state_types import AgentInput
from app.agent.public_view import format_partial_progress, public_conversation_messages
from tests.test_agent_tool_argument_ingress import PROTOCOLS, Wire, encode

A, B, C = "library.search", "library.health", "library.inspect"
X = {"ref": "object-x", "scope": "owner-a"}
Z = {"ref": "object-z"}
FINAL = "对象核验有据完成，保留模型正常分析。"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("real sockets and DNS are forbidden")
    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, deny)
    for name in ("getaddrinfo", "create_connection"):
        monkeypatch.setattr(socket, name, deny)


@pytest.fixture
def store(tmp_path):
    previous = db.DB_PATH, bool(getattr(db, "_configured_test_mode", False))
    db.configure_database(tmp_path / "recovered-progress.db", test_mode=True)
    db.init_db()
    try:
        yield SQLiteKernelStore(secret_provider=lambda: "offline-recovered-progress")
    finally:
        db.configure_database(previous[0], test_mode=previous[1])


def reply(protocol, *, name="", arguments=None, index=0, text=FINAL, private=False):
    cid = f"call-{index}"
    if protocol == "chat_completions":
        message: dict[str, Any] = {"content": "" if name else text}
        if private:
            message["reasoning_content"] = f"PRIVATETHINKING{index}"
        if name:
            message["tool_calls"] = [{"type": "function", "id": cid, "function": {
                "name": name, "arguments": encode(arguments),
            }}]
        payload = {"choices": [{"message": message, "finish_reason": "tool_calls" if name else "stop"}]}
    elif protocol == "responses":
        output = [{"type": "function_call", "id": f"item-{index}", "call_id": cid,
                   "name": name, "arguments": encode(arguments)}] if name else [
            {"type": "message", "content": [{"type": "output_text", "text": text}]},
        ]
        payload = {"status": "completed", "output": output}
        if private:
            output.insert(0, {"type": "reasoning", "id": f"rs_{index}",
                              "encrypted_content": f"PRIVATEOPAQUE{index}"})
    else:
        content = [{"type": "tool_use", "id": cid, "name": name, "input": arguments}] if name else [
            {"type": "text", "text": text},
        ]
        payload = {"content": content, "stop_reason": "tool_use" if name else "end_turn"}
        if private:
            content.insert(0, {"type": "thinking", "thinking": f"PRIVATETHINKING{index}",
                               "signature": f"PRIVATESIGNATURE{index}"})
    return encode(payload).encode(), "application/json"


async def run_scenario(store, steps, *, protocol="chat_completions", ending="answer", summaries=None, private=False):
    """Finite wire replies; only the read business boundary is synthesized."""
    calls = []
    pending = list(steps)

    def read(arguments, context):
        name, expected, outcome = pending.pop(0)
        assert dict(arguments) == expected
        assert context.owner == "owner-a"
        calls.append((name, dict(arguments), outcome))
        summary = (summaries or {}).get(len(calls) - 1, "未恢复对象:" + str(expected.get("ref", name)))
        if isinstance(outcome, str):
            raise ToolPipelineError(summary, code=outcome)
        if isinstance(outcome, dict):
            return outcome
        return {"ok": True, "status": "success", "summary": "已核验:" + str(expected.get("ref", name)),
                "data": {"items": [{"title": "可信已得内容", "year": "2026"}]}}

    catalog = ToolCatalog([KernelToolSpec(
        name=name, domain="library", description="核验对象状态", effect=ToolEffect.READ,
        input_schema={"type": "object", "properties": {}, "additionalProperties": True}, read=read,
    ) for name in sorted({step[0] for step in steps})])
    replies = [reply(protocol, name=catalog.get(name).model_name, arguments=args, index=index, private=private)
               for index, (name, args, _) in enumerate(steps)]
    limits = None
    if ending == "model-fault":
        replies.append((b"not-json", "application/json"))
    elif ending == "round-empty":
        limits = SessionLimits(max_model_rounds=len(steps))
    elif ending == "tool-empty":
        limits = SessionLimits(max_tool_calls=len(steps))
        replies.append(reply(protocol, name=catalog.get(A).model_name, arguments=X, index=len(steps)))
    elif ending == "round-blocked":
        limits = SessionLimits(max_tool_calls=len(steps))
        replies.append(reply(protocol, name=catalog.get(A).model_name, arguments=X, index=len(steps)))
    else:
        replies.append(reply(protocol))
    wire = Wire(protocol, replies)
    session = AgentSession(
        model=wire.adapter, catalog=catalog,

        pipeline=ToolPipeline(catalog=catalog, state_store=store), state_store=store, limits=limits,
    )
    events = [event async for event in session.run(AgentInput(
        message="分别核验所有对象，只报告有证据完成项。", owner="owner-a", session_id="recovery",
    ))]
    wire.verify(posts=len(steps) + (ending != "round-empty"))
    assert not wire.replies
    assert calls == steps
    fresh = SQLiteKernelStore(secret_provider=lambda: "offline-recovered-progress")
    saved = await fresh.load(owner="owner-a", session_id="recovery")
    final = dict(events[-1].payload)
    assert saved.conversation[-1]["content"] == final["answer"]
    # Recovery is not permission to delete original tool messages or events.
    tools = [row for row in saved.conversation if row["role"] == "tool"]
    expected_tool_count = len(steps) + (ending in {"round-blocked", "tool-empty"})
    assert len(tools) == expected_tool_count
    assert [row["tool_call_id"] for row in tools] == [f"call-{i}" for i in range(expected_tool_count)]
    failures = [e for e in events if e.type.value == "tool.failed"]
    assert len(failures) == sum(isinstance(step[2], str) for step in steps) + (ending in {"round-blocked", "tool-empty"})
    return final, saved, wire, events


def unfinished(answer):
    return next((line for line in answer.splitlines() if line.startswith("未完成的查询：")), "")


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_recovered_request_not_unfinished_in_public_or_reopened_sqlite(store, protocol):
    steps = [(A, X, "rate_limited"), (C, Z, "provider_timeout"), (B, {}, None), (A, X, None)]
    final, saved, wire, _ = asyncio.run(run_scenario(store, steps, protocol=protocol))
    assert final["status"] == "partial"
    assert final["finish_reason"] == ("end_turn" if protocol == "anthropic_messages" else "stop")
    assert "已有工具返回真实频率限制" not in encode(wire.requests[-1])
    for answer in (final["answer"], saved.conversation[-1]["content"]):
        assert answer == FINAL
    assert "未恢复对象:object-z" in encode(wire.requests[-1])
    assert "可信已得内容" in encode(wire.requests[-1])


@pytest.mark.parametrize("case", [
    "single", "all", "repeated", "reordered", "empty-same", "unrelated", "other-ref", "other-scope", "empty-switched",
])
def test_existing_identity_recovery_controls(store, case):
    steps = [(A, X, "rate_limited"), (B, {}, None)]
    if case == "single":
        steps = [(A, X, "rate_limited"), (A, X, None)]
    elif case == "all":
        steps += [(C, Z, "provider_timeout"), (A, X, None), (C, Z, None)]
    elif case == "repeated":
        steps += [(A, X, "provider_timeout"), (A, X, None)]
    elif case == "reordered":
        steps += [(A, {"scope": "owner-a", "ref": "object-x"}, None)]
    elif case == "empty-same":
        steps = [(A, {}, "rate_limited"), (A, {}, None)]
    elif case == "other-ref":
        steps += [(A, {**X, "ref": "object-y"}, None)]
    elif case == "other-scope":
        steps += [(A, {**X, "scope": "owner-b"}, None)]
    elif case == "empty-switched":
        steps = [(A, {}, "rate_limited"), (B, {}, None), (A, {}, None)]
    final, _, wire, _ = asyncio.run(run_scenario(store, steps))
    recovered = case in {"single", "all", "repeated", "reordered", "empty-same"}
    assert final["status"] == ("success" if recovered else "partial")
    assert final["answer"] == FINAL
    assert final["finish_reason"] == "stop"
    assert "已有工具返回真实频率限制" not in encode(wire.requests[-1])


@pytest.mark.parametrize("ending,reason", [
    ("model-fault", "model_provider_error"), ("round-empty", "model_round_budget_exceeded"),
    ("tool-empty", "tool_budget_exceeded"), ("round-blocked", "tool_budget_exceeded"),
])
@pytest.mark.parametrize("unresolved", [False, True])
def test_fallback_entries_prioritize_current_errors_over_budget_status(store, ending, reason, unresolved):
    steps = [(A, X, "rate_limited")]
    if unresolved:
        steps.append((C, Z, "provider_timeout"))
    steps += [(B, {}, None), (A, X, None)]
    final, _, wire, _ = asyncio.run(run_scenario(store, steps, ending=ending))
    assert final["status"] == "partial"
    if unresolved and ending != "model-fault":
        reason = "provider_timeout"
    assert final["finish_reason"] == reason
    assert unfinished(final["answer"]) == ("未完成的查询：未恢复对象:object-z" if unresolved else "")
    assert "可信已得内容" in final["answer"]
    assert "已有工具返回真实频率限制" not in encode(wire.requests[-1])


@pytest.mark.parametrize("recover_between", [False, True])
def test_repeated_failure_uses_latest_message_and_ledger_order(store, recover_between):
    steps = [(A, X, "rate_limited"), (C, Z, "provider_timeout"), (B, {}, None)]
    if recover_between:
        steps.append((A, X, None))
    steps.append((A, X, "provider_timeout"))
    final, _, wire, _ = asyncio.run(run_scenario(
        store, steps, ending="round-empty",
        summaries={0: "第一版错误", 1: "仍缺少另一项", len(steps) - 1: "第二版错误"},
    ))
    assert final["status"] == "partial"
    assert final["finish_reason"] == "provider_timeout"
    assert "第一版错误" not in final["answer"]
    assert unfinished(final["answer"]) == "未完成的查询：" + (
        "仍缺少另一项；第二版错误" if recover_between else "第二版错误；仍缺少另一项"
    )
    assert "已有工具返回真实频率限制" not in encode(wire.requests[-1])


@pytest.mark.parametrize("other", [{**X, "ref": "object-y"}, {**X, "scope": "owner-b"}])
def test_identical_summaries_do_not_infer_recovery_for_another_identity(store, other):
    steps = [(A, X, "rate_limited"), (A, other, "provider_timeout"), (B, {}, None), (A, X, None)]
    final, _, wire, _ = asyncio.run(run_scenario(store, steps, ending="round-empty", summaries={0: "对象检查未完成", 1: "对象检查未完成"}))
    assert final["status"] == "partial"
    assert final["finish_reason"] == "provider_timeout"
    assert unfinished(final["answer"]) == "未完成的查询：对象检查未完成"
    assert "已有工具返回真实频率限制" not in encode(wire.requests[-1])


@pytest.mark.parametrize("ok", [False, True])
def test_returned_rate_limit_is_covered_by_existing_ledger(store, ok):
    steps = [(A, X, {"ok": ok, "status": "rate_limited", "summary": "旧限制"}),
             (C, Z, "provider_timeout"), (B, {}, None), (A, X, None)]
    final, _, _, _ = asyncio.run(run_scenario(store, steps, ending="round-empty"))
    assert final["status"] == "partial"
    assert final["finish_reason"] == "provider_timeout"
    assert unfinished(final["answer"]) == "未完成的查询：未恢复对象:object-z"
    assert "旧限制" not in final["answer"]


@pytest.mark.parametrize("status", ["failed", "error", "not_found"])
def test_failed_public_results_without_ledger_entry_remain_visible(store, status):
    # 真实中断仍展示尚未恢复的公开失败，不丢失原始 DTO。
    steps = [(A, X, "rate_limited"), (C, Z, {"ok": False, "status": status, "summary": "历史公开失败"}),
             (B, {}, None), (A, X, None)]
    final, _, _, _ = asyncio.run(run_scenario(store, steps, ending="model-fault"))
    assert final["status"] == "partial"
    assert final["finish_reason"] == "model_provider_error"
    assert unfinished(final["answer"]) == "未完成的查询：历史公开失败"


@pytest.mark.parametrize("error_text", [
    "检查不可用 library.search provider_state", "检查不可用 /srv/private/error",
    "检查不可用 access_token=SYNTHETICSECRETVALUE",
])
def test_current_error_redaction_and_parameter_identity_never_escape_to_answers(store, error_text):
    private_args = {"ref": "object-z", "scope": "PRIVATEIDENTITY", "absolute_path": "/srv/private/identity"}
    steps = [(A, X, "rate_limited"), (C, private_args, "provider_timeout"), (B, {}, None), (A, X, None)]
    final, saved, _, events = asyncio.run(run_scenario(
        store, steps, ending="round-empty", summaries={1: error_text},
    ))
    assert final["status"] == "partial"
    assert final["finish_reason"] == "provider_timeout"
    if error_text == "检查不可用 library.search provider_state":
        assert "检查不可用" in unfinished(final["answer"])
    public_messages = public_conversation_messages(saved.conversation)
    for text in (final["answer"], saved.conversation[-1]["content"], encode(public_messages)):
        for forbidden in ("PRIVATEIDENTITY", "/srv/private/", "provider_state", "_recovery_tracked", "SYNTHETICSECRETVALUE"):
            assert forbidden not in text
    # 展开执行过程需要结构化 method；用户可见正文、标签和摘要仍须去掉内部工具名。
    visible_texts = [final["answer"], saved.conversation[-1]["content"]]
    for message in public_messages:
        visible_texts.append(message["content"])
        for activity in message.get("activities", []):
            visible_texts.extend((activity.get("label", ""), activity.get("summary", "")))
    assert all("library.search" not in text for text in visible_texts)
    assert any(activity.get("method") == "library.search"
               for message in public_messages for activity in message.get("activities", []))
    assert "_recovery_tracked" not in encode([dict(event.payload) for event in events])


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_projection_leaves_native_tool_state_intact_and_private(store, protocol):
    steps = [(A, X, "rate_limited"), (C, Z, "provider_timeout"), (B, {}, None), (A, X, None)]
    final, saved, wire, events = asyncio.run(run_scenario(store, steps, protocol=protocol, private=True))
    assert final["status"] == "partial"
    assert final["answer"] == FINAL
    assert "未恢复对象:object-z" in encode(wire.requests[-1])
    states = [row["provider_state"] for row in saved.conversation if row.get("provider_state")]
    assert len(states) == len(steps)
    for index, state in enumerate(states):
        layout = json.loads(state["layout_json"])
        if protocol == "anthropic_messages":
            assert {"type": "thinking", "thinking": f"PRIVATETHINKING{index}",
                    "signature": f"PRIVATESIGNATURE{index}"} in layout
            assert f"PRIVATESIGNATURE{index}" in encode(wire.requests[-1])
        elif protocol == "responses":
            assert any(block.get("encrypted_content") == f"PRIVATEOPAQUE{index}" for block in layout)
            assert f"PRIVATEOPAQUE{index}" in encode(wire.requests[-1])
        else:
            assert f"PRIVATETHINKING{index}" in encode(layout)
            assert f"PRIVATETHINKING{index}" in encode(wire.requests[-1])
    public = encode([dict(event.payload) for event in events]) + encode(public_conversation_messages(saved.conversation))
    for secret in ("PRIVATETHINKING", "PRIVATESIGNATURE", "PRIVATEOPAQUE"):
        assert secret not in public


@pytest.mark.parametrize("options", [{}, {"write_attempted": True}, {
    "confirmed_result": {"ok": True, "status": "submitted", "summary": "请求已提交"},
}])
def test_formatter_without_current_errors_keeps_historical_contract(options):
    history = [("检查", {"ok": False, "summary": "历史错误"}), ("检查", {"ok": True, "summary": "后续成功"})]
    text = format_partial_progress("故障收尾", history, **options)
    assert "历史错误" in text
    assert "后续成功" in text
    if options:
        assert "本轮工具核对结果" in text
        assert "不会自动重放" in text
    else:
        assert unfinished(text) == "未完成的查询：历史错误"


@pytest.mark.parametrize("receipt_status,ending", [
    ("submitted", "answer"), ("success", "answer"), ("success", "model-fault"), ("submitted", "round-empty"),
])
def test_confirmed_history_is_not_filtered_or_replayed(store, receipt_status, ending):
    async def run():
        executed = []
        attempts = []

        def execute(*_):
            executed.append("write")
            return {"ok": True, "status": receipt_status, "summary": "可信执行回执"}

        def read(*_):
            attempts.append("read")
            if len(attempts) == 1:
                raise ToolPipelineError("历史检查失败", code="provider_timeout")
            return {"ok": True, "summary": "恢复检查成功"}

        write = KernelToolSpec(name="cloud.submit", domain="cloud", description="提交",
            input_schema={"type": "object", "properties": {}}, effect=ToolEffect.WRITE,
            prepare=lambda *_: PreparedEffect(preview={"summary": "待确认预览"}, snapshot_fingerprint="snapshot"),
            execute_confirmed=execute)
        lookup = KernelToolSpec(name=A, domain="library", description="核验",
            input_schema={"type": "object", "properties": {}}, effect=ToolEffect.READ, read=read)
        catalog = ToolCatalog([write, lookup])
        wire = Wire("chat_completions", [
            reply("chat_completions", name=write.model_name, arguments={}, index=0),
            reply("chat_completions", name=lookup.model_name, arguments=X, index=1),
            reply("chat_completions", name=lookup.model_name, arguments=X, index=2),
            (b"not-json", "application/json") if ending == "model-fault" else reply(
                "chat_completions", text="" if ending == "round-empty" else FINAL,
            ),
        ])
        session = AgentSession(model=wire.adapter, catalog=catalog,

            pipeline=ToolPipeline(catalog=catalog, state_store=store), state_store=store,
            limits=SessionLimits(max_model_rounds=3) if ending == "round-empty" else None)
        preview = [e async for e in session.run(AgentInput(message="提交后继续核验", owner="owner-a", session_id="confirmed"))]
        assert executed == []
        plan_id = preview[-1].payload["plan_id"]
        events = [e async for e in session.confirm(owner="owner-a", session_id="confirmed", plan_id=plan_id)]
        assert executed == ["write"]
        assert attempts == ["read", "read"]
        final = events[-1].payload
        if receipt_status == "submitted" or ending == "model-fault":
            assert "历史检查失败" in final["answer"]
            assert "恢复检查成功" in final["answer"]
            assert "可信执行回执" in final["answer"]
        else:
            assert final["answer"] == FINAL
        fresh = SQLiteKernelStore(secret_provider=lambda: "offline-recovered-progress")
        saved = await fresh.load(owner="owner-a", session_id="confirmed")
        assert saved.conversation[-1]["content"] == final["answer"]
        duplicate = [e async for e in session.confirm(owner="owner-a", session_id="confirmed", plan_id=plan_id)]
        assert duplicate[-1].payload["code"] == "confirmation_invalid"
        assert executed == ["write"]
        wire.verify(posts=3 if ending == "round-empty" else 4)
    asyncio.run(run())
