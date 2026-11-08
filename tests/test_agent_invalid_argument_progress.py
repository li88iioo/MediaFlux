"""READ语法可直接修正；真实服务错误仍阻止把不完整查询当成完成。"""
from __future__ import annotations

import asyncio
import copy
import json
import socket
from types import MappingProxyType
from typing import Any

import pytest

from app import database as db
from app.agent.errors import ToolPipelineError
from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.projection import ToolOutcome
from app.agent.kernel.session import AgentSession, SessionLimits
from app.agent.kernel.state_types import AgentInput
from app.agent.public_view import public_conversation_messages
from tests.test_agent_tool_argument_ingress import Wire, encode

A, B, C = "library.lookup", "library.health", "library.inspect"
X = {"ref": "object-x", "scope": "scope-a"}
Z = {"ref": "object-z"}
FINAL = "仅按工具证据说明核验结果。"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("real sockets and DNS are forbidden")
    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, deny)
    for name in ("getaddrinfo", "create_connection", "gethostbyname", "gethostbyname_ex", "gethostbyaddr"):
        monkeypatch.setattr(socket, name, deny)


@pytest.fixture
def store(tmp_path):
    previous = db.DB_PATH, bool(getattr(db, "_configured_test_mode", False))
    db.configure_database(tmp_path / "invalid-argument-progress.db", test_mode=True)
    db.init_db()
    try:
        yield SQLiteKernelStore(secret_provider=lambda: "offline-invalid-progress")
    finally:
        db.configure_database(previous[0], test_mode=previous[1])


def reply(*, name="", arguments=None, index=0, text=FINAL):
    message: dict[str, Any] = {"content": "" if name else text}
    if name:
        message["tool_calls"] = [{"type": "function", "id": f"call-{index}", "function": {
            "name": name, "arguments": encode(arguments),
        }}]
    return encode({"choices": [{"message": message, "finish_reason": "tool_calls" if name else "stop"}]}).encode(), "application/json"


async def scenario(store, good, *, ending="answer", mixed=True):
    bad = {**X, "limit": "bad"}
    steps = [(A, bad)]
    if mixed:
        steps += [(C, Z), (B, {})]
    steps.append((A, good))
    calls = []
    returned = []

    def reader(name):
        def read(arguments, context):
            assert context.owner == "owner-a"
            calls.append((name, dict(arguments)))
            if name == C:
                raise ToolPipelineError("对象z超时", code="provider_timeout")
            title = "已核验 " + arguments["ref"] + " " + arguments["scope"] if name == A else "仅健康检查资料"
            value = {"ok": True, "status": "success", "summary": title, "data": {"items": [{"title": title}]}}
            returned.append((value, copy.deepcopy(value)))
            return ToolOutcome(model_content=encode(value), public_content=MappingProxyType(value))
        return read

    catalog = ToolCatalog([KernelToolSpec(
        name=name, domain="library", description="核验对象", effect=ToolEffect.READ,
        input_schema={"type": "object", "properties": {"limit": {"type": "integer"}}} if name == A else {
            "type": "object", "properties": {},
        }, read=reader(name),
    ) for name in sorted({name for name, _ in steps})])
    replies = [reply(name=catalog.get(name).model_name, arguments=args, index=index)
               for index, (name, args) in enumerate(steps)]
    limits = None
    if ending == "round-empty":
        limits = SessionLimits(max_model_rounds=len(steps))
    elif ending == "tool-empty":
        limits = SessionLimits(max_tool_calls=len(steps))
        replies.append(reply(name=catalog.get(steps[0][0]).model_name,
                             arguments=steps[0][1], index=len(steps)))
    else:
        replies.append((b"not-json", "application/json") if ending == "model-fault" else reply(text=FINAL))
    wire = Wire("chat_completions", replies)
    session = AgentSession(
        model=wire.adapter, catalog=catalog,
        pipeline=ToolPipeline(catalog=catalog, state_store=store), state_store=store, journal=store, limits=limits,
    )
    events = [event async for event in session.run(AgentInput(
        message="分别核验对象与作用域，不以另一对象成功冒充恢复。", owner="owner-a", session_id="invalid-progress",
    ))]
    wire.verify(posts=len(steps) + (ending != "round-empty"))
    assert not wire.replies
    # A's invalid request is rejected by the real schema, before the business boundary.
    assert calls == steps[1:]
    assert (A, bad) not in calls
    assert calls[-1] == (A, good)
    assert all(value == original for value, original in returned)
    fresh = SQLiteKernelStore(secret_provider=lambda: "offline-invalid-progress")
    saved = await fresh.load(owner="owner-a", session_id="invalid-progress")
    journal = await fresh.list_events(owner="owner-a", session_id="invalid-progress")
    assert journal == [event.to_dict() for event in events]
    tool_rows = [row for row in saved.conversation if row["role"] == "tool"]
    assert [row["tool_call_id"] for row in tool_rows] == [f"call-{i}" for i in range(len(steps) + (ending == "tool-empty"))]
    rejection = json.loads(tool_rows[0]["content"])
    assert rejection["code"] == "invalid_arguments"
    assert "repair" not in rejection
    assert "expected_input_schema" in rejection
    assert "类型无效" in rejection["error"]
    assert json.loads(tool_rows[len(steps) - 1]["content"])["status"] == "success"
    failed = [event for event in events if event.type.value == "tool.failed"]
    assert len(failed) == (2 if mixed else 1) + (ending == "tool-empty")
    final = dict(events[-1].payload)
    answers = {
        "event": final["answer"],
        "sqlite": saved.conversation[-1]["content"],
        "public": public_conversation_messages(saved.conversation)[-1]["content"],
    }
    assert journal[-1]["payload"]["answer"] == final["answer"]
    assert all(answer == final["answer"] for answer in answers.values())
    assert "_recovery_tracked" not in encode(journal) + encode(saved.conversation) + encode(wire.requests)
    return final, answers


@pytest.mark.parametrize("other", [
    {**X, "ref": "object-y", "limit": 1}, {**X, "scope": "scope-b", "limit": 1},
], ids=["different-ref", "different-scope-same-owner"])
@pytest.mark.parametrize("ending,reason", [
    ("answer", "stop"), ("model-fault", "model_provider_error"),
    ("round-empty", "provider_timeout"), ("tool-empty", "provider_timeout"),
])
def test_operational_failure_remains_disclosed_after_read_schema_correction(store, other, ending, reason):
    final, answers = asyncio.run(scenario(store, other, ending=ending))
    assert final["status"] == "partial"
    assert final["finish_reason"] == reason
    if ending == "answer":
        assert all(answer == FINAL for answer in answers.values())
        return
    for projection, answer in answers.items():
        assert "已核验 " + other["ref"] + " " + other["scope"] in answer, projection
        assert "已核验 object-x scope-a" not in answer, projection
        assert "未完成的查询不能据此判断没有内容" in answer, projection
        unfinished = next(line for line in answer.splitlines() if line.startswith("未完成的查询："))
        assert "对象z超时" in unfinished, projection
        assert "类型无效" not in unfinished, (projection, "syntax feedback must not remain a terminal blocker", answer)


@pytest.mark.parametrize("mixed", [False, True])
def test_direct_read_correction_retains_wire_history_and_does_not_hide_operational_failures(store, mixed):
    final, answers = asyncio.run(scenario(store, {**X, "limit": 1}, mixed=mixed))
    assert final["status"] == ("partial" if mixed else "success")
    assert final["finish_reason"] == "stop"
    assert all(answer == FINAL for answer in answers.values())
