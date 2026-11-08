"""Bounded public decoding through real returned results and reopened consumers."""
from __future__ import annotations

import asyncio
import copy
import json
import socket
from urllib.parse import quote

import pytest

from app import database as db
from app.agent.kernel.adapters import TurnViewBuilder, iter_ndjson
from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.events import AgentEvent, AgentEventType
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.projection import DefaultProjector
from app.agent.kernel.session import AgentSession, SessionLimits
from app.agent.kernel.state_types import AgentInput
from app.agent.models import ToolResult
from app.agent.public_safety import (
    replace_internal_identifiers,
    sanitize_public_text,
    sanitize_resource_title,
)
from app.agent.public_view import public_conversation_messages
from tests.test_agent_tool_argument_ingress import PROTOCOLS, Wire, encode

SUMMARY = "稍后重试即可"
FALLBACK = "工具调用未取得完整结果。"
PRIVATE = {
    "path": "／srv／private／S1PATHCANARY",
    "url": "ｈｔｔｐｓ：／／private.invalid／S1URLCANARY",
    "credential": "ａｃｃｅｓｓ＿ｔｏｋｅｎ＝S1CREDENTIALCANARY",
}


def escaped(text, depth, *, interleaved=False):
    for _ in range(depth):
        text = quote(text, safe="")
        if interleaved:
            text = text.replace("%", "％", 1)
    return text


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
    db.configure_database(tmp_path / "public-decoding.db", test_mode=True)
    db.init_db()
    try:
        yield SQLiteKernelStore(secret_provider=lambda: "offline-public-decoding")
    finally:
        db.configure_database(previous[0], test_mode=previous[1])


def reply(protocol, *, name="", index=0):
    cid, args = f"call-{index}", {"ref": "healthy" if index else "limited"}
    if protocol == "chat_completions":
        message = {"content": ""}
        if name:
            message = {"tool_calls": [{"type": "function", "id": cid, "function": {"name": name, "arguments": encode(args)}}]}
        body = {"choices": [{"message": message, "finish_reason": "tool_calls" if name else "stop"}]}
    elif protocol == "responses":
        item = {"type": "function_call", "id": f"item-{index}", "call_id": cid, "name": name, "arguments": encode(args)} if name else {
            "type": "message", "content": [{"type": "output_text", "text": ""}],
        }
        body = {"status": "completed", "output": [item]}
    else:
        item = {"type": "tool_use", "id": cid, "name": name, "input": args} if name else {"type": "text", "text": ""}
        body = {"content": [item], "stop_reason": "tool_use" if name else "end_turn"}
    return encode(body).encode(), "application/json"


async def assert_scoped_snapshot(pipeline, handle, expected, *, owner, session_id):
    """真实会话快照全文读取；路径选择不改变原节点类型。"""
    from dataclasses import replace
    from unittest.mock import AsyncMock

    from app.agent.errors import ToolPipelineError
    from app.agent.kernel.session_service import SessionService
    from app.agent.kernel.state_types import CancellationToken
    from app.agent.kernel.tool_calls import ToolCallContext
    lease, _ = await pipeline.state_store.begin_turn(owner=owner, session_id=session_id, request_id="snapshot-read")
    context = ToolCallContext(owner=owner, session_id=session_id, request_id=lease.request_id,
        turn_id=lease.turn_id, lease=lease, cancellation=CancellationToken(), report_progress=AsyncMock())
    for wrong in (replace(context, owner=owner + "-other"), replace(context, session_id=session_id + "-other")):
        with pytest.raises(ToolPipelineError) as denied:
            await SessionService(pipeline, wrong).result(handle)
        assert denied.value.code == "reference_invalid"

    async def check(path, value):
        result = await SessionService(pipeline, context).result(handle, path)
        assert result["result_handle"] == handle
        assert result["ok"] is True and result["status"] == "snapshot"
        assert result["data"] == value
        if isinstance(value, (dict, list)):
            children = value.items() if isinstance(value, dict) else enumerate(value)
            for key, child in children:
                await check(path + "/" + str(key).replace("~", "~0").replace("/", "~1"), child)

    await check("", expected)


async def observe(store, error, protocol, mixed):
    value = ToolResult(False, "rate_limited", SUMMARY, error=error)
    original = copy.deepcopy(value)
    calls = []

    def read(arguments, context):
        assert context.owner == "decoding-owner"
        calls.append(dict(arguments))
        return value if arguments["ref"] == "limited" else ToolResult(
            True, "success", "独立成功事实", data={"items": [{"title": "可信已得资料"}]},
        )

    tool = KernelToolSpec(
        name="library.lookup", domain="library", description="只读核验", effect=ToolEffect.READ,
        input_schema={"type": "object", "properties": {"ref": {"type": "string"}}, "required": ["ref"], "additionalProperties": False},
        read=read,
    )
    catalog = ToolCatalog([tool])
    replies = [reply(protocol, name=tool.model_name)]
    if mixed:
        replies.append(reply(protocol, name=tool.model_name, index=1))
    wire = Wire(protocol, replies)
    # Exercise the sanitizer's input boundary, not the independent DTO compactor.
    projector = DefaultProjector(max_model_chars=100_000)
    session = AgentSession(
        model=wire.adapter, catalog=catalog,
        pipeline=ToolPipeline(catalog=catalog, state_store=store, projector=projector), state_store=store, journal=store,
        # 工具错误投影在真实预算中断验证；正常模型链接与路径不走该净化器。
        limits=SessionLimits(max_model_rounds=len(replies)),
    )
    emitted = [event async for event in session.run(AgentInput(message="核验并如实交付", owner="decoding-owner", session_id="decoding"))]
    assert calls == [{"ref": "limited"}] + ([{"ref": "healthy"}] if mixed else [])
    assert value == original
    wire.verify(posts=len(replies))
    assert not wire.replies
    fresh = SQLiteKernelStore(secret_provider=lambda: "offline-public-decoding")
    saved = await fresh.load(owner="decoding-owner", session_id="decoding")
    journal = await fresh.list_events(owner="decoding-owner", session_id="decoding")
    assert journal == [event.to_dict() for event in emitted]
    completed = [event for event in journal if event["type"] == "tool.completed"]
    assert len(completed) == len(calls)
    # Raw returned audit is deliberately not rewritten to hide synthetic canaries.
    audit = completed[0]["payload"]["result"]
    if "result_handle" in audit:
        handle = audit["result_handle"]
        assert handle.startswith("ref_")
        assert audit == {**value.to_dict(), "refs": [], "result_handle": handle, "read_method": "session.result"}
        # The raw journal remains exact; the snapshot is the complete model-safe DTO.
        from app.agent.kernel.projection import _model_safe
        await assert_scoped_snapshot(session.pipeline, handle, _model_safe(value.to_model_dict()),
                                     owner="decoding-owner", session_id="decoding")
    else:
        assert audit == value.to_dict()
    assert not any(event["type"] == "tool.failed" for event in journal)
    assert "已有工具返回真实频率限制" not in encode(wire.requests[-1])
    events = [AgentEvent(type=AgentEventType(event["type"]), sequence=event["sequence"], request_id=event["request_id"], session_id=event["session_id"], turn_id=event["turn_id"], occurred_at=event["occurred_at"], event_id=event["event_id"], payload=event["payload"]) for event in journal]
    builder = TurnViewBuilder()
    for event in events:
        builder.apply(event)

    async def records():
        for event in events:
            yield event

    lines = [line async for line in iter_ndjson(records())]
    assert [json.loads(line) for line in lines] == journal
    terminal = json.loads(lines[-1])
    view = builder.build().to_dict()
    final = terminal["payload"]
    if mixed:
        assert terminal["type"] == "turn.completed"
        assert final["status"] == view["status"] == "partial"
        assert final["finish_reason"] == "rate_limited"
        assert final["answer"] == saved.conversation[-1]["content"] == public_conversation_messages(saved.conversation)[-1]["content"]
        assert "可信已得资料" in final["answer"]
    else:
        assert terminal["type"] == "turn.failed" and view["status"] == "failed"
        assert view["error"] == {"code": "rate_limited", "message": final["message"]}
    return final, view, terminal


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("kind", PRIVATE)
def test_original_s1_raw_terminal_consumers(store, protocol, kind):
    final, view, terminal = asyncio.run(observe(store, escaped(PRIVATE[kind], 1), protocol, False))
    assert final["code"] == "rate_limited"
    # Only final public fields: historical tool DTOs are a different boundary.
    assert "CANARY" not in encode([view["error"], terminal])


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("mixed", [False, True], ids=["raw-failed", "mixed-partial"])
@pytest.mark.parametrize("kind", PRIVATE)
@pytest.mark.parametrize("depth,interleaved", [(0, False), (1, False), (2, False), (4, False), (8, False), (9, False), (64, False), (3, True), (8, True), (9, True)])
def test_nested_private_errors_never_fall_back_to_summary(store, protocol, mixed, kind, depth, interleaved):
    error = escaped(PRIVATE[kind], depth, interleaved=interleaved)
    final, view, terminal = asyncio.run(observe(store, error, protocol, mixed))
    text = final["answer"] if mixed else final["message"]
    assert "CANARY" not in text and SUMMARY not in text
    if mixed:
        assert "未完成的查询：" not in text
    else:
        assert text == FALLBACK
        assert "CANARY" not in encode([view["error"], terminal])


@pytest.mark.parametrize("mixed", [False, True])
@pytest.mark.parametrize("length", [599, 600, 601, 65535, 65536, 65537])
def test_full_input_is_checked_before_terminal_truncation(store, mixed, length):
    # ASCII keeps the 64K-character boundary below the independent 128K-byte journal cap.
    error = "a" * length + " " + escaped(PRIVATE["credential"], 1)
    final, _, _ = asyncio.run(observe(store, error, "chat_completions", mixed))
    text = final["answer"] if mixed else final["message"]
    assert "a" not in text and "CANARY" not in text and SUMMARY not in text
    if not mixed:
        assert text == FALLBACK


SAFE = [
    ("对象Ａ：请等待６０秒", "对象A:请等待60秒"),
    ("已完成５０％，等待２０％", "已完成50%,等待20%"),
    ("合法百分号 % %2 %GG", "合法百分号 % %2 %GG"),
    ("速度 12 MiB/s", "速度 12 MiB/s"),
    (escaped("已完成50%", 8), "已完成50%"),
    (escaped("对象Ａ：请等待６０秒", 3, interleaved=True), "对象A:请等待60秒"),
]


@pytest.mark.parametrize("raw,expected", SAFE)
def test_safe_text_decodes_fully_and_stays_idempotent(raw, expected):
    assert sanitize_public_text(raw) == expected
    assert sanitize_public_text(expected) == expected
    assert replace_internal_identifiers(raw) == expected


@pytest.mark.parametrize("raw,expected", SAFE)
@pytest.mark.parametrize("mixed", [False, True])
def test_safe_terminal_controls(store, raw, expected, mixed):
    final, _, _ = asyncio.run(observe(store, raw, "chat_completions", mixed))
    if mixed:
        assert "未完成的查询：" + expected in final["answer"]
    else:
        assert final["message"] == expected


@pytest.mark.parametrize("length", [65535, 65536, 65537])
def test_decode_input_budget_and_safe_prefix(length):
    expected = "安" * 600 if length <= 65536 else ""
    assert sanitize_public_text("安" * length) == expected
    assert replace_internal_identifiers("安" * length) == ("安" * length if length <= 65536 else "")


@pytest.mark.parametrize("sanitizer", [sanitize_public_text, sanitize_resource_title, replace_internal_identifiers])
@pytest.mark.parametrize("raw", [escaped("普通中文", 9), escaped("普通中文", 9, interleaved=True), "%FF", "\ufdfa" * 4000])
def test_unparseable_or_expanding_inputs_fail_closed(sanitizer, raw):
    assert sanitizer(raw) == ""


@pytest.mark.parametrize("kind", PRIVATE)
@pytest.mark.parametrize("length", [199, 200, 201, 299, 300, 301, 599, 600, 601])
def test_public_and_resource_checks_precede_truncation(kind, length):
    raw = "安" * length + " " + escaped(PRIVATE[kind], 1)
    assert sanitize_public_text(raw, limit=200) == ""
    # Resource titles intentionally mask URLs; paths and credentials reject the whole title.
    expected = ("安" * length + " [网址]")[:200].rstrip() if kind == "url" else ""
    assert sanitize_resource_title(raw, limit=200) == expected


def test_resource_title_url_masking_is_an_existing_contract():
    assert sanitize_resource_title("安全标题 https://private.invalid/S1URLCANARY") == "安全标题 [网址]"


@pytest.mark.parametrize("length", [199, 200, 201, 600])
def test_resource_title_direct_path_suffix_is_not_truncated_before_check(length):
    assert sanitize_resource_title("安" * length + " /srv/private/S1PATHCANARY", limit=200) == ""


@pytest.mark.parametrize("raw,expected", [
    ("Film.Name_2026.1080p.mkv", "Film.Name_2026.1080p.mkv"),
    ("<b>电影Ａ</b> ５０％", "电影A 50%"),
    (escaped("电影Ａ ５０％", 3), "电影A 50%"),
])
def test_resource_titles_keep_filename_punctuation(raw, expected):
    assert sanitize_resource_title(raw) == expected


@pytest.mark.parametrize("name", [
    "test.txt", "changed.txt", "Film.Name_2026.mkv", "Spider-Man-No-Way-Home.mkv",
    "requestId.txt", "mf-ordinary-file.txt", "library.search.mkv", "Film_Name.mkv",
])
def test_public_explanation_preserves_whole_business_names(name):
    text = f"当前文件 `{name}`,分享尚未创建。"
    assert sanitize_public_text(text) == text
    assert replace_internal_identifiers(text) == text
    assert sanitize_public_text(f"请使用 {name} 核对名称") == f"请使用 {name} 核对名称"


@pytest.mark.parametrize("name", ["cloud.list", "cloud__list"])
def test_native_and_domain_tool_names_share_public_label(name):
    from app.agent.public_safety import public_tool_label
    assert public_tool_label(name) == "光鸭文件查询"
    assert sanitize_public_text(f"查询方式：{name}") == "查询方式:光鸭文件查询"
    assert name not in sanitize_public_text(f"建议调用 {name} 继续处理。")


def test_partial_failure_keeps_actual_filename_and_failed_receipt():
    from app.agent.public_view import format_partial_progress
    text = format_partial_progress(
        "分享对象已变化", [],
        confirmed_result={"ok": False, "status": "failed", "summary": "分享对象已变化,请重新读取目录"},
        analysis_text="原名称 `test.txt`，当前名称 `changed.txt`。",
    )
    assert "`test.txt`" in text and "`changed.txt`" in text
    assert "内部检查" not in text
    assert "❌" in text


@pytest.mark.parametrize("unsafe", [
    "test.txt access_token=PRIVATECANARY123456", "changed.txt /srv/private/file.env",
    "test.txt https://private.invalid/token", "test.txt C:\\private\\file.env",
])
def test_business_names_do_not_bypass_whole_text_secret_and_path_checks(unsafe):
    assert sanitize_public_text(unsafe) == ""


def test_bare_opaque_identifier_is_not_a_filename():
    assert sanitize_public_text("mf-private-snapshot") == "内部检查"
    assert sanitize_public_text("DO-NOT-PUBLISH") == "内部检查"
    assert sanitize_public_text("mf-private-snapshot.txt") == "mf-private-snapshot.txt"
