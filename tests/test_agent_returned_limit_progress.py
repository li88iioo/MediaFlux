"""返回限流是模型观察；仅真实中断/写入收尾使用未恢复事实兜底。"""
from __future__ import annotations

import asyncio
import copy
import json
import socket

import pytest

from app import database as db
from app.agent.errors import ToolPipelineError
from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.session import AgentSession, SessionLimits
from app.agent.kernel.state_types import AgentInput
from app.agent.models import ToolResult
from app.agent.public_view import public_conversation_messages
from tests.test_agent_recovered_progress import reply, unfinished
from tests.test_agent_tool_argument_ingress import PROTOCOLS, Wire, encode

A, B, C = "library.lookup", "library.health", "library.inspect"
X, Z = {"ref": "object-x"}, {"ref": "object-z"}
DETAIL = "对象丙被限流,请等待六十秒再核验"
SUMMARY = "对象检查受到限制"
PRIVATE = "检查失败 /srv/private/returned-limit"
FINAL = "已按证据完成核验。"
ENDINGS = [
    ("answer", "stop"), ("fault", "model_provider_error"),
    ("round-empty", "rate_limited"), ("tool-empty", "rate_limited"),
]


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
    db.configure_database(tmp_path / "returned-limit.db", test_mode=True)
    db.init_db()
    try:
        yield SQLiteKernelStore(secret_provider=lambda: "offline-returned-limit")
    finally:
        db.configure_database(previous[0], test_mode=previous[1])


async def scenario(store, steps, *, ending="answer", protocol="chat_completions"):
    seen, returned = [], []

    def reader(name):
        def read(arguments, context):
            expected_name, expected_args, value = steps[len(seen)]
            assert context.owner == "owner-a"
            assert (name, dict(arguments)) == (expected_name, expected_args)
            seen.append((name, dict(arguments)))
            if isinstance(value, ToolPipelineError):
                raise value
            if value is None:
                value = ToolResult(True, "success", "已核验", data={"items": [{"title": "可信已得内容"}]})
            returned.append((value, copy.deepcopy(value)))
            return value
        return read

    catalog = ToolCatalog([KernelToolSpec(
        name=name, domain="library", description="只读对象核验", effect=ToolEffect.READ,
        input_schema={"type": "object", "properties": {
            "ref": {"type": "string"}, "scope": {"type": "string"},
        }, "additionalProperties": False}, read=reader(name),
    ) for name in sorted({step[0] for step in steps})])
    replies = [reply(protocol, name=catalog.get(name).model_name, arguments=args, index=index)
               for index, (name, args, _) in enumerate(steps)]
    limits = None
    if ending == "round-empty":
        limits = SessionLimits(max_model_rounds=len(steps))
    elif ending == "tool-empty":
        limits = SessionLimits(max_tool_calls=len(steps))
        replies.append(reply(protocol, name=catalog.get(steps[0][0]).model_name,
                             arguments=steps[0][1], index=len(steps)))
    else:
        replies.append((b"not-json", "application/json") if ending == "fault" else reply(protocol, text=FINAL))
    wire = Wire(protocol, replies)
    session = AgentSession(
        model=wire.adapter, catalog=catalog,
        pipeline=ToolPipeline(catalog=catalog, state_store=store), state_store=store, journal=store, limits=limits,
    )
    events = [event async for event in session.run(AgentInput(
        message="分别核验所有对象，保留未完成的真实原因。", owner="owner-a", session_id="returned-limit",
    ))]
    assert seen == [(name, args) for name, args, _ in steps]
    assert all(value == original for value, original in returned)
    wire.verify(posts=len(steps) + (ending != "round-empty"))
    assert not wire.replies
    fresh = SQLiteKernelStore(secret_provider=lambda: "offline-returned-limit")
    saved = await fresh.load(owner="owner-a", session_id="returned-limit")
    journal = await fresh.list_events(owner="owner-a", session_id="returned-limit")
    assert journal == [event.to_dict() for event in events]
    tools = [row for row in saved.conversation if row["role"] == "tool"]
    assert [row["tool_call_id"] for row in tools] == [f"call-{index}" for index in range(len(steps) + (ending == "tool-empty"))]
    completed = [event for event in events if event.type.value == "tool.completed"]
    assert len(completed) == len(returned)
    # A returned failure stays a returned DTO in audit; it is not a thrown failure.
    for event, (value, _) in zip(completed, returned):
        original = value.to_dict() if isinstance(value, ToolResult) else value
        assert dict(event.payload)["result"] == original
    failures = [event for event in events if event.type.value == "tool.failed"]
    assert len(failures) == sum(isinstance(value, ToolPipelineError) for _, _, value in steps) + (ending == "tool-empty")
    final = dict(events[-1].payload)
    public = public_conversation_messages(saved.conversation)
    if events[-1].type.value == "turn.completed":
        assert final["answer"] == saved.conversation[-1]["content"] == public[-1]["content"]
        assert final["answer"] == journal[-1]["payload"]["answer"]
    assert "_recovery_tracked" not in encode(journal) + encode(saved.conversation) + encode(wire.requests)
    # Model DTOs independently redact paths; the ledger must not rewrite replay.
    assert "/srv/private" not in encode(wire.requests) + encode(saved.conversation) + encode(public)
    return final, tools, wire, events


def mixed(outcome):
    return [(A, X, ToolPipelineError("对象甲旧限流", code="rate_limited")),
            (C, Z, outcome), (B, {}, None), (A, X, None)]


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("ok", [False, True])
@pytest.mark.parametrize("summary", [SUMMARY, PRIVATE], ids=["safe-summary", "private-summary"])
@pytest.mark.parametrize("ending,reason", ENDINGS)
def test_unrecovered_returned_error_priority_survives_every_projection(store, protocol, ok, summary, ending, reason):
    outcome = ToolResult(ok, "rate_limited", summary, error=DETAIL)
    final, tools, wire, _ = asyncio.run(scenario(store, mixed(outcome), ending=ending, protocol=protocol))
    assert final["status"] == "partial"
    assert final["finish_reason"] == ("end_turn" if ending == "answer" and protocol == "anthropic_messages" else reason)
    assert json.loads(tools[1]["content"])["error"] == DETAIL
    assert DETAIL in encode(wire.requests[-1])
    assert "已有工具返回真实频率限制" not in encode(wire.requests[-1])
    if ending == "answer":
        assert final["answer"] == FINAL
        return
    assert "可信已得内容" in final["answer"]
    assert "对象甲旧限流" not in final["answer"]
    assert unfinished(final["answer"]) == "未完成的查询：" + DETAIL


def test_primary_returned_limit_detail_survives_more_than_three_errors(store):
    errors = [DETAIL, "对象丁被限流", "对象戊被限流", "对象己被限流"]
    steps = [(C, {"ref": f"object-{index}"}, ToolResult(False, "rate_limited", SUMMARY, error=error))
             for index, error in enumerate(errors)] + [(B, {}, None)]
    final, _, _, _ = asyncio.run(scenario(store, steps, ending="round-empty"))

    assert final["status"] == "partial" and final["finish_reason"] == "rate_limited"
    assert DETAIL in final["answer"].splitlines()[0]
    assert final["answer"].count(DETAIL) == 1
    assert unfinished(final["answer"]) == "未完成的查询：" + "；".join(errors[-3:])
    assert "可信已得内容" in final["answer"]


@pytest.mark.parametrize("ok", [False, True])
@pytest.mark.parametrize("kind", ["absent", "empty", "unsafe"])
@pytest.mark.parametrize("summary", [SUMMARY, PRIVATE], ids=["safe-summary", "private-summary"])
@pytest.mark.parametrize("ending,reason", ENDINGS)
def test_missing_empty_and_unsafe_errors_do_not_bypass_safe_filter(store, ok, kind, summary, ending, reason):
    outcome = ToolResult(ok, "rate_limited", summary, error=PRIVATE if kind == "unsafe" else "")
    if kind == "absent":
        outcome = outcome.to_dict()
        outcome.pop("error")
    final, _, wire, _ = asyncio.run(scenario(store, mixed(outcome), ending=ending))
    assert final["status"] == "partial" and final["finish_reason"] == reason
    assert "已有工具返回真实频率限制" not in encode(wire.requests[-1])
    if ending == "answer":
        assert final["answer"] == FINAL
        return
    expected = SUMMARY if summary == SUMMARY and kind != "unsafe" else ""
    assert unfinished(final["answer"]) == ("未完成的查询：" + expected if expected else "")
    assert "/srv/private" not in final["answer"]
    # A nonempty but unsafe error must not cause a second fallback to safe summary.
    if kind == "unsafe":
        assert SUMMARY not in final["answer"]


@pytest.mark.parametrize("ok", [False, True])
@pytest.mark.parametrize("ending,reason", ENDINGS)
def test_returned_error_recovery_removes_only_current_request(store, ok, ending, reason):
    outcome = ToolResult(ok, "rate_limited", SUMMARY, error="对象甲旧限流详情")
    steps = [(A, X, outcome), (C, Z, ToolResult(False, "rate_limited", SUMMARY, error=DETAIL)),
             (B, {}, None), (A, X, None)]
    final, tools, wire, _ = asyncio.run(scenario(store, steps, ending=ending))
    assert final["status"] == "partial" and final["finish_reason"] == reason
    assert "对象甲旧限流详情" in tools[0]["content"]
    assert "对象甲旧限流详情" in encode(wire.requests[-1])
    assert "对象甲旧限流详情" not in final["answer"]
    if ending == "answer":
        assert final["answer"] == FINAL
        return
    assert unfinished(final["answer"]) == "未完成的查询：" + DETAIL


@pytest.mark.parametrize("recover_between", [False, True])
def test_repeated_returned_error_uses_latest_detail_in_ledger_order(store, recover_between):
    steps = [(A, X, ToolResult(False, "rate_limited", SUMMARY, error="第一版限流详情")),
             (C, Z, ToolResult(False, "rate_limited", SUMMARY, error=DETAIL)), (B, {}, None)]
    if recover_between:
        steps.append((A, X, None))
    steps.append((A, X, ToolResult(False, "rate_limited", SUMMARY, error="第二版限流详情")))
    final, tools, wire, _ = asyncio.run(scenario(store, steps, ending="round-empty"))
    assert final["status"] == "partial" and final["finish_reason"] == "rate_limited"
    expected = DETAIL + "；第二版限流详情" if recover_between else "第二版限流详情；" + DETAIL
    assert unfinished(final["answer"]) == "未完成的查询：" + expected
    assert "第一版限流详情" not in final["answer"]
    assert "第一版限流详情" in tools[0]["content"] + encode(wire.requests[-1])


@pytest.mark.parametrize("other", [{"ref": "object-y", "scope": "scope-a"}, {"ref": "object-x", "scope": "scope-b"}])
def test_same_summary_different_request_never_infers_recovery(store, other):
    original = {**X, "scope": "scope-a"}
    steps = [(A, original, ToolResult(False, "rate_limited", SUMMARY, error="仅对象甲旧详情")),
             (A, other, ToolResult(False, "rate_limited", SUMMARY, error=DETAIL)),
             (B, {}, None), (A, original, None)]
    final, _, wire, _ = asyncio.run(scenario(store, steps, ending="round-empty"))
    assert final["status"] == "partial" and final["finish_reason"] == "rate_limited"
    assert unfinished(final["answer"]) == "未完成的查询：" + DETAIL
    assert "仅对象甲旧详情" not in final["answer"]
    assert "已有工具返回真实频率限制" not in encode(wire.requests[-1])


@pytest.mark.parametrize("ok", [False, True])
@pytest.mark.parametrize("error", [DETAIL, PRIVATE], ids=["safe-error", "unsafe-error"])
@pytest.mark.parametrize("ending", ["answer", "round-empty"])
def test_returned_limit_without_success_keeps_observation_or_interrupted_fallback(store, ok, error, ending):
    final, tools, wire, events = asyncio.run(scenario(store, [(C, Z, ToolResult(ok, "rate_limited", SUMMARY, error=error))], ending=ending))
    assert "已有工具返回真实频率限制" not in encode(wire.requests[-1])
    assert json.loads(tools[0]["content"])["status"] == "rate_limited"
    if ending == "answer":
        assert events[-1].type.value == "turn.completed"
        assert final["status"] == "partial" and final["finish_reason"] == "stop"
        assert final["answer"] == FINAL
    elif ok:
        assert events[-1].type.value == "turn.completed"
        assert final["status"] == "partial" and final["finish_reason"] == "rate_limited"
        assert unfinished(final["answer"]) == ("未完成的查询：" + DETAIL if error == DETAIL else "")
    else:
        assert events[-1].type.value == "turn.failed"
        assert final["code"] == "rate_limited"
        assert final["message"] == (DETAIL if error == DETAIL else "工具调用未取得完整结果。")
    assert "/srv/private" not in encode(final)
