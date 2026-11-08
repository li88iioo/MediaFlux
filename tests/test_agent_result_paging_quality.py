"""Real Session identities and lossless budget boundaries, with no remote model."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from app.agent.errors import ToolPipelineError
from app.agent.kernel.capabilities import (
    ToolCatalog,
)
from app.agent.kernel.conversation_state import retain_conversation
from app.agent.kernel.model import (
    ModelMessage,
    ModelToolCall,
)
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.projection import DefaultProjector
from app.agent.kernel.session_service import SessionService
from app.agent.kernel.state_types import CancellationToken
from app.agent.kernel.tool_calls import ToolCallContext
from app.agent.model_context_budget import _compact_chain, compact_tool_content
from tests.support import isolated_test_database


async def legacy_reader_message(*, provider_alias):
    # 迁移前的持久页与工具绑定；仅作为历史解码输入，不重新注册退役运行时。
    store = SQLiteKernelStore(secret_provider=lambda: "paging-quality-test")
    lease, _ = await store.begin_turn(owner="quality", session_id="alias", request_id="original")
    context = ToolCallContext(owner="quality", session_id="alias", request_id=lease.request_id,
        turn_id=lease.turn_id, lease=lease, cancellation=CancellationToken(), report_progress=AsyncMock())
    pipeline = ToolPipeline(catalog=ToolCatalog([]), state_store=store, reference_store=store)
    source = {"data": {"items": [{"id": i, "description": str(i) * 1000} for i in range(8)]}}
    handle = await pipeline.save_result(json.dumps(source), context)
    reader_name = "agent__read_result" if provider_alias else "agent.read_result"
    page = {"ok": True, "status": "success", "result_handle": handle, "read_tool": "agent.read_result",
        "data": {"path": "/data/items", "kind": "list", "offset": 3, "total": 8, "items": [source["data"]["items"][3]],
                 "returned": 1, "has_more": True, "next_offset": 4}}
    message = ModelMessage(role="tool", tool_call_id="page", tool_name=reader_name, content=json.dumps(page))
    caller = ModelMessage(role="assistant", tool_calls=(ModelToolCall("page", reader_name,
        {"handle": handle, "path": "/data/items", "offset": 3, "limit": 1}),))
    await store.commit(lease, conversation=[caller.to_dict(), message.to_dict()])
    cold = SQLiteKernelStore(secret_provider=lambda: "paging-quality-test")
    saved = await cold.load(owner="quality", session_id="alias")
    assert ModelMessage.from_dict(saved.conversation[0]) == caller
    assert ModelMessage.from_dict(saved.conversation[1]) == message
    return message, caller, cold


@pytest.mark.parametrize("provider_alias", [True, False], ids=["provider-alias", "canonical"])
@pytest.mark.parametrize("route", ["model", "history"])
def test_legacy_page_coordinates_after_sqlite_reopen(provider_alias, route):
    async def run():
        original, caller, store = await legacy_reader_message(provider_alias=provider_alias)
        if route == "model":
            chain = _compact_chain([caller, original], max_tool_chars=800)
            assert chain[0] == caller
            message = chain[1]
            assert len(message.content) <= 800
        else:
            # Independent retention: never feed model-compacted content into this branch.
            rows = retain_conversation([original.to_dict()], maximum_bytes=1000)
            assert len(rows) == 1
            message = ModelMessage.from_dict(rows[0])
        assert replace(message, content=original.content) == original
        lease, _ = await store.begin_turn(owner="quality", session_id="alias", request_id="verify")
        await store.commit(lease, conversation=[caller.to_dict(), message.to_dict()])
        reopened = SQLiteKernelStore(secret_provider=lambda: "paging-quality-test")
        saved = await reopened.load(owner="quality", session_id="alias")
        assert ModelMessage.from_dict(saved.conversation[0]) == caller
        assert ModelMessage.from_dict(saved.conversation[1]) == message
        view = json.loads(saved.conversation[1]["content"])
        handle = json.loads(original.content)["result_handle"]
        assert view["result_handle"] == handle
        assert view["read_tool"] == "agent.read_result"
        assert view["data"]["offset"] == 3
        assert view["data"]["returned"] == 1
        assert view["data"]["total"] == 8
        assert len(view["truncation"]) == 1
        row = view["truncation"][0]
        assert row["path"] == "/data/items/3/description"
        assert row["total"] == 1000
        assert row["next_offset"] == row["returned"]
        prefix = view["data"]["items"][0]["description"]
        assert len(prefix) == row["returned"]
        reader = ToolPipeline(catalog=ToolCatalog([]), state_store=reopened, reference_store=reopened,
                              projector=DefaultProjector(max_model_chars=2000))
        context = ToolCallContext(owner="quality", session_id="alias", request_id=lease.request_id,
                                  turn_id=lease.turn_id, lease=lease, cancellation=CancellationToken(),
                                  report_progress=AsyncMock())
        original_node = await SessionService(reader, context).result(handle, row["path"])
        assert original_node["result_handle"] == handle
        assert prefix + original_node["data"][row["next_offset"]:] == "3" * 1000

    with isolated_test_database():
        asyncio.run(run())


@pytest.mark.parametrize("metadata", [{}, {"truncated": False, "truncation": []}])
def test_lossless_pretty_json_preserves_complete_semantics(metadata):
    payload = {"ok": True, "data": {"items": list(range(80)), "label": "空 白  \n 原样"}, **metadata}
    original = ModelMessage(role="tool", tool_name="library.inventory", content=json.dumps(payload, indent=2))
    assert len(original.content) > 400
    assert len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"))) <= 400
    result = _compact_chain([original], max_tool_chars=400)[0]
    assert json.loads(result.content) == payload
    assert len(result.content) <= 400
    assert replace(result, content=original.content) == original
    assert _compact_chain([result], max_tool_chars=400) == [result]


def test_valid_arguments_survive_real_sqlite_retention_losslessly():
    from app.modules.episode_audit import normalize_library_update_arguments

    arguments = {"queries": [str(i).zfill(2) + "x" * 92 for i in range(20)], "refresh": True}
    arguments["queries"][0] += "y" * 12
    assert normalize_library_update_arguments(arguments)
    assert len(json.dumps(arguments, ensure_ascii=False)) == 2002
    assert len(json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))) <= 2000

    async def run():
        store = SQLiteKernelStore(secret_provider=lambda: "quality-arguments")
        lease, _ = await store.begin_turn(owner="quality", session_id="arguments", request_id="fresh")
        caller = ModelMessage(role="assistant", tool_calls=(ModelToolCall("batch", "library__check_updates", arguments),))
        receipt = ModelMessage(role="tool", tool_call_id="batch", tool_name="library__check_updates", content='{"ok":true}')
        rows = [ModelMessage(role="user", content="核对片单").to_dict(),
                ModelMessage(role="assistant", content="A" * 170000).to_dict(),
                caller.to_dict(), receipt.to_dict()]
        assert len(json.dumps(rows).encode()) > 160 * 1024
        await store.commit(lease, conversation=rows)
        saved = await SQLiteKernelStore(secret_provider=lambda: "quality-arguments").load(
            owner="quality", session_id="arguments",
        )
        assert len(saved.conversation) == len(rows)
        assert len(saved.conversation[1]["content"]) < 170000
        assert ModelMessage.from_dict(saved.conversation[2]) == caller
        assert ModelMessage.from_dict(saved.conversation[3]) == receipt
        assert saved.conversation[2]["tool_calls"][0]["arguments"] == arguments
        assert rows[1]["content"] == "A" * 170000

    with isolated_test_database():
        asyncio.run(run())


@pytest.mark.parametrize("name", ["library__inventory", "agent_read_result", "agent__read_result_extra"])
def test_page_shaped_business_result_does_not_grant_reader_identity(name):
    payload = {"ok": True, "result_handle": "ordinary-snapshot", "read_tool": "agent.read_result", "data": {
        "path": "/catalog", "kind": "list", "offset": 3, "total": 8, "returned": 1,
        "has_more": True, "next_offset": 4, "items": [{"description": "D" * 1000}],
    }}
    message = ModelMessage(role="tool", tool_name=name, content=json.dumps(payload))
    view = json.loads(_compact_chain([message], max_tool_chars=800)[0].content)
    assert view["truncation"][0]["path"] == "/data/items/0/description"
    assert view["data"]["path"] == "/catalog"
    assert view["data"]["offset"] == 3


@pytest.mark.parametrize("name", ["agent.read_result", "agent__read_result", "library__inventory"])
def test_lossless_reencoding_still_rejects_bad_trusted_metadata(name):
    # This fits after removing whitespace; invalid metadata must not use that escape.
    payload = {"ok": True, "truncated": True, "data": {"items": list(range(80))}}
    message = ModelMessage(role="tool", tool_name=name, content=json.dumps(payload, indent=2))
    assert len(message.content) > 400
    assert len(json.dumps(payload, separators=(",", ":"))) <= 400
    with pytest.raises(ToolPipelineError) as raised:
        _compact_chain([message], max_tool_chars=400)
    assert raised.value.code == "invalid_tool_result"


def test_reencoding_preserves_prior_loss_and_records_business_collisions():
    row = {"path": "/data/description", "kind": "string", "offset": 0, "total": 1000,
           "returned": 1, "has_more": True, "next_offset": 1}
    payload = {"ok": True, "data": {"description": "D"}, "truncated": True, "truncation": [row]}
    # Whitespace removal must neither clear an inherited obligation nor invent one.
    raw = json.dumps(payload, indent=8)
    assert len(raw) > 400
    assert json.loads(compact_tool_content(raw, maximum=400, trusted=True)) == payload
    source = {"truncation": None, "data": {"description": "D"}}
    view = json.loads(compact_tool_content(json.dumps(source), maximum=1000))
    assert view["data"] == source["data"]
    assert view["truncated"] is True
    assert view["truncation"] == [{"path": "/truncation", "kind": "scalar", "total": 1,
                                   "offset": 0, "returned": 0, "has_more": True,
                                   "next_offset": 0, "scope": "subtree"}]
