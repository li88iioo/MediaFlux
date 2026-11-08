"""既有快照 → 宿主会话绑定 → 原生工作区全文；无服务重查或第二套归档。"""
import asyncio
import json
import os
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from app import sdk
from app.agent.errors import ToolPipelineError
from app.agent.kernel import references, sandbox, telegram_reference
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.model import ModelMessage
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.references import InMemoryReferenceStore
from app.agent.kernel.session_service import SessionService
from app.agent.kernel.state import InMemorySessionStateStore
from app.agent.kernel.state_types import CancellationToken
from app.agent.kernel.tool_calls import ToolCallContext
from tests.support import isolated_test_database


@pytest.fixture
def native_workspace(tmp_path, monkeypatch):
    if not sandbox.sandbox_supported():
        pytest.skip("需要可运行 Bubblewrap 的 Linux 隔离工作区")
    monkeypatch.setattr(sandbox, "get_runtime_paths", lambda: SimpleNamespace(
        data_dir=tmp_path, runtime_dir=tmp_path / "runtime"))


async def context(store, owner="owner", session="history"):
    lease, _ = await store.begin_turn(owner=owner, session_id=session, request_id="read")
    return ToolCallContext(owner=owner, session_id=session, request_id=lease.request_id,
        turn_id=lease.turn_id, lease=lease, cancellation=CancellationToken(), report_progress=AsyncMock())


@pytest.mark.parametrize("persistent", [False, True], ids=["memory", "sqlite"])
@pytest.mark.parametrize("concurrent", [False, True], ids=["sequential", "concurrent"])
def test_small_pointers_load_one_bounded_snapshot_per_workspace(persistent, concurrent, monkeypatch):
    async def run():
        state: SQLiteKernelStore | InMemorySessionStateStore
        if persistent:
            refs = SQLiteKernelStore(secret_provider=lambda: "snapshot-cache-test")
            state = refs
        else:
            refs = InMemoryReferenceStore()
            state = InMemorySessionStateStore()
        pipeline = ToolPipeline(catalog=ToolCatalog([]), state_store=state, reference_store=refs)
        ctx = await context(state)
        content = json.dumps({"items": [{"id": f"item-{i}", "text": "x" * 2000} for i in range(1000)]})
        handle = await pipeline.save_result(content, ctx)
        manifest = await refs.resolve(handle, owner=ctx.owner, session_id=ctx.session_id, expected_kind="tool_result")
        counts = {"reads": 0, "bytes": 0, "decodes": 0}

        def observe(record):
            value = record[0]
            counts["reads"] += 1
            counts["bytes"] += len((value if isinstance(value, str) else json.dumps(value)).encode())
            return record

        if isinstance(refs, SQLiteKernelStore):
            read_sync = refs._resolve_ref_sync
            def counted_sync(*args, **kwargs):
                return observe(read_sync(*args, **kwargs))
            monkeypatch.setattr(refs, "_resolve_ref_sync", counted_sync)
        else:
            read_async = refs._read
            async def counted_async(*args, **kwargs):
                return observe(await read_async(*args, **kwargs))
            monkeypatch.setattr(refs, "_read", counted_async)

        def decode(value):
            counts["decodes"] += 1
            return json.loads(value)
        monkeypatch.setattr(references, "json", SimpleNamespace(loads=decode))
        service = SessionService(pipeline, ctx)
        paths = [f"/items/{i}/id" for i in range(10)]
        results = (await asyncio.gather(*(service.result(handle, path) for path in paths)) if concurrent
                   else [await service.result(handle, path) for path in paths])
        for i, (path, result) in enumerate(zip(paths, results)):
            assert result == {
                "ok": True, "status": "snapshot", "result_handle": handle,
                "path": path, "source_kind": "tool_result", "data": f"item-{i}",
            }
        assert counts["decodes"] == 1, counts
        assert counts["reads"] <= 2 * len(manifest["parts"]) + 10, counts
        assert counts["bytes"] < 3 * len(content.encode()), counts
        print(f"snapshot persistent={persistent} concurrent={concurrent}: {counts}")
        # 新工作区必须重新加载，快照不能驻留在全局或仓储中。
        assert (await SessionService(pipeline, ctx).result(handle, "/items/0/id"))["data"] == "item-0"
        assert counts["decodes"] == 2
    with isolated_test_database():
        asyncio.run(run())


@pytest.mark.parametrize("persistent", [False, True], ids=["memory", "sqlite"])
@pytest.mark.parametrize("expires", ["manifest", "part"])
def test_cached_snapshot_keeps_scope_and_earliest_part_ttl(persistent, expires, monkeypatch):
    async def run():
        clock = [0.0]
        started = references.monotonic()
        monkeypatch.setattr(references, "monotonic", lambda: started + clock[0])
        state: SQLiteKernelStore | InMemorySessionStateStore
        if persistent:
            refs = SQLiteKernelStore(secret_provider=lambda: "snapshot-ttl-test", clock=lambda: clock[0])
            state = refs
        else:
            refs = InMemoryReferenceStore(clock=lambda: clock[0])
            state = InMemorySessionStateStore()
        ctx = await context(state)
        scope = {"owner": ctx.owner, "session_id": ctx.session_id}
        first = await refs.put(**scope, kind="tool_result_part", value='{"items":', ttl_seconds=30)
        last = await refs.put(**scope, kind="tool_result_part", value='[{"id": "saved"}]}',
                              ttl_seconds=10 if expires == "part" else 30)
        manifest = await refs.put(**scope, kind="tool_result", value={"parts": [first.ref, last.ref]},
                                  ttl_seconds=10 if expires == "manifest" else 30)
        pipeline = ToolPipeline(catalog=ToolCatalog([]), state_store=state, reference_store=refs)
        service = SessionService(pipeline, ctx)
        assert (await service.result(manifest.ref, "/items/0/id"))["data"] == "saved"
        for foreign in (replace(ctx, owner="other"), replace(ctx, session_id="other")):
            service.context = foreign
            with pytest.raises(ToolPipelineError) as error:
                await service.result(manifest.ref, "/items/0/id")
            assert error.value.code == "reference_invalid"
        service.context = ctx
        assert (await service.result(manifest.ref, "/items/0/id"))["data"] == "saved"
        clock[0] = 11
        with pytest.raises(ToolPipelineError) as error:
            await service.result(manifest.ref, "/items/0/id")
        assert error.value.code == "reference_invalid"
    with isolated_test_database():
        asyncio.run(run())


def test_snapshot_cache_is_single_bounded_and_returns_independent_nodes(monkeypatch):
    async def run():
        state = InMemorySessionStateStore()
        pipeline = ToolPipeline(catalog=ToolCatalog([]), state_store=state)
        ctx = await context(state)
        first = await pipeline.save_result(json.dumps({"items": [{"id": "first"}]}), ctx)
        second = await pipeline.save_result(json.dumps({"items": [{"id": "second"}]}), ctx)
        large = await pipeline.save_result(json.dumps({"text": "正文" * 100}), ctx)
        decode = Mock(wraps=json.loads)
        monkeypatch.setattr(references, "json", SimpleNamespace(loads=decode))
        monkeypatch.setattr(references, "_RESULT_CACHE_CHARS", 64)
        service = SessionService(pipeline, ctx)
        node = (await service.result(first, "/items/0"))["data"]
        node["id"] = "changed"
        assert (await service.result(first, "/items/0"))["data"] == {"id": "first"}
        assert decode.call_count == 1
        assert (await service.result(second, "/items/0/id"))["data"] == "second"
        assert (await service.result(first, "/items/0/id"))["data"] == "first"
        assert decode.call_count == 3  # 换目标后不保留旧快照。
        for _ in range(2):
            assert (await service.result(large, "/text"))["data"] == "正文" * 100
        assert decode.call_count == 5  # 超限结果沿原链读取，不留在缓存中。
    asyncio.run(run())


def test_sdk_binding_is_scoped_and_rpc_archives_whole_result(tmp_path):
    async def run():
        store = InMemorySessionStateStore()
        pipeline = ToolPipeline(catalog=ToolCatalog([]), state_store=store)
        original = {"items": list(range(2000)), "text": "正文" * 2000, "ok": False, "status": "unknown"}
        ctx = replace(await context(store), current_task={"task_id": "task", "epoch": 1, "revision": 1,
            "status": "partial", "obligations": [{"acceptance": {"kind": "observed_read"}, "state": "pending"}]})
        from app.agent.kernel.task_state import read_observations
        assert read_observations(ctx.current_task, "library.test", {}, {"ok": True, "status": "success"})
        handle = await pipeline.save_result(json.dumps(original), ctx)
        assert all(t.name != "agent.read_result" for t in pipeline.catalog.visible())
        with pytest.raises(ValueError):
            sdk.session.result(handle)
        async def request(scope, name):
            root = tmp_path / name
            (root / "run").mkdir(parents=True)
            (root / "output").mkdir()
            rpc = sandbox._SDKRequests(scope, root, pipeline)
            rpc.uid = os.getuid()
            (root / "run/mf-requests/req.json").write_text(json.dumps({
                "method": "session.result", "arguments": {"handle": handle}}))
            try:
                await rpc.dispatch("req.json")
                response = json.loads((root / "run/mf-responses/req.json").read_text())
                assert rpc.patches == []  # 读取历史不是新业务完成证据
            finally:
                assert await rpc.close() is None
            files = list((root / "output").glob("*.sdk.json"))
            assert len(files) == 1 and json.loads(files[0].read_text()) == response
            return response
        good, foreign = await asyncio.gather(request(ctx, "good"), request(replace(ctx, owner="foreign"), "foreign"))
        assert good["result"]["data"] == original
        assert foreign["error"]["code"] == "reference_invalid"
        with pytest.raises(ValueError):
            sdk.session.result(handle)
    asyncio.run(run())


def test_pointer_errors_missing_chunks_and_cancellation():
    async def run():
        store = InMemorySessionStateStore()
        pipeline = ToolPipeline(catalog=ToolCatalog([]), state_store=store)
        ctx = await context(store)
        original: dict[str, Any] = {"a/b": {"~x": [None, False, 7, "全文"]}, "empty": [], "键" * 1200: "原长键全文"}
        handle = await pipeline.save_result(json.dumps(original), ctx)
        service = SessionService(pipeline, ctx)
        for path in ("bad", "/a~2b", "/a~1b/~0x/-1", "/a~1b/~0x/01", "/missing", "/a~1b/~0x/0/missing"):
            with pytest.raises(ToolPipelineError) as error:
                await service.result(handle, path)
            assert error.value.code == "invalid_arguments"
        assert (await service.result(handle))["data"] == original
        assert (await service.result(handle, "/" + "键" * 1200))["data"] == "原长键全文"
        for i, value in enumerate(original["a/b"]["~x"]):
            assert (await service.result(handle, f"/a~1b/~0x/{i}"))["data"] == value
        broken = await pipeline.reference_store.put(owner=ctx.owner, session_id=ctx.session_id,
            kind="tool_result", value={"parts": ["ref_missing"]}, ttl_seconds=900)
        with pytest.raises(ToolPipelineError) as error:
            await service.result(broken.ref)
        assert error.value.code == "reference_invalid"
        ctx.cancellation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await service.result(handle)
    asyncio.run(run())


def test_full_snapshot_cold_workspace_python_cli_and_file_readback(native_workspace):
    async def run():
        original = {"items": [{"id": i, "text": "长字段" * 500} for i in range(180)],
                    "receipt": {"ok": False, "status": "unknown", "plan_id": "saved-plan-only"}}
        store = SQLiteKernelStore(secret_provider=lambda: "session-result-test")
        ctx = await context(store)
        pipe = ToolPipeline(catalog=ToolCatalog([]), state_store=store, reference_store=store)
        handle = await pipe.save_result(json.dumps(original, ensure_ascii=False), ctx)
        # 迁移前 read_tool 引用：原数据与旧历史不重写，冷进程读相同分片。
        await store.commit(ctx.lease, conversation=[{"role": "tool", "tool_name": "legacy.query", "content": json.dumps({
            "result_handle": handle, "read_tool": "agent.read_result", "truncated": True})}])
        async def execute(code, language="python", owner="owner", sid="history"):
            cold = SQLiteKernelStore(secret_provider=lambda: "session-result-test")
            current = await context(cold, owner, sid)
            catalog = ToolCatalog([])
            pipeline = ToolPipeline(catalog=catalog, state_store=cold, reference_store=cold)
            catalog.register(sandbox.run_code_tool(pipeline))
            result = await pipeline.execute("agent.run_code", {"code": code, "language": language}, context=current)
            data = json.loads(result.outcome.full_model_content or result.outcome.model_content)["data"]
            assert data["exit_code"] == 0, data.get("stderr", data)
            return data
        first = await execute(f'''from mf import session
import json
try:
    session.result({handle!r}, path="/missing")
except Exception as exc:
    assert exc.code == "invalid_arguments"
else:
    raise AssertionError("bad path accepted")
v = session.result({handle!r})
assert len(v["data"]["items"]) == 180
json.dump(v["data"], open("snapshot.json", "w"), ensure_ascii=False)
print(v["data"]["receipt"]["status"], len(v["data"]["items"][-1]["text"]))''')
        assert first["stdout"].strip() == "unknown 1500"
        second = await execute('''import json, glob
v = json.load(open("snapshot.json"))
archived = [json.load(open(p)) for p in glob.glob("/output/*.sdk.json")]
assert any(r.get("result", {}).get("data") == v for r in archived)
assert len(v["items"]) == 180 and v["items"][-1]["text"] == "长字段" * 500
print(v["receipt"]["plan_id"])''')
        assert second["stdout"].strip() == "saved-plan-only"
        help_result = await execute('mf session.result --help', "shell")
        assert "handle" in help_result["stdout"] and "path" in help_result["stdout"]
        foreign = await execute(f'''from mf import session
try:
    session.result({handle!r})
except Exception as exc:
    assert exc.code == "reference_invalid"
    print("isolated")
else:
    raise AssertionError("foreign snapshot leaked")''', owner="other")
        assert foreign["stdout"].strip() == "isolated"
        cold = SQLiteKernelStore(secret_provider=lambda: "session-result-test")
        saved = await cold.load(owner="owner", session_id="history")
        assert json.loads(saved.conversation[0]["content"])["read_tool"] == "agent.read_result"
    with isolated_test_database():
        asyncio.run(run())


@pytest.mark.parametrize("legacy", [False, True])
def test_expired_telegram_preview_downgrades_without_rewriting_history(legacy):
    async def run():
        store = InMemorySessionStateStore()
        clock = [0]
        refs = InMemoryReferenceStore(clock=lambda: clock[0])
        pipe = ToolPipeline(catalog=ToolCatalog([]), state_store=store, reference_store=refs)
        ctx = await context(store)
        reply = await telegram_reference.prepare({"text": "原文" * 1500, "source": {
            "kind": "text", "chat_id": "test", "message_id": 7}}, ctx.lease, pipe.reference_store)
        handle = reply["reference_handle"]
        assert isinstance(handle, str)
        preview = telegram_reference.preview(reply)
        if legacy:
            preview = preview.replace('"read_method": "session.result"', '"read_tool": "agent.read_result"')
        user = ModelMessage(role="user", content=preview)
        page = ModelMessage(role="tool", tool_name="agent.read_result", content=json.dumps({
            "untrusted_reference": True, "source": reply["source"], "result_handle": handle,
            "read_tool": "agent.read_result", "data": {"items": "旧已读事实"}}))
        await store.commit(ctx.lease, conversation=[{**user.to_dict(), "reply_context": reply}, page.to_dict()])
        assert (await SessionService(pipe, ctx).result(handle))["data"] == {"text": reply["text"]}
        clock[0] = 901
        projected = await telegram_reference.project_history((user, page), [reply], current_handle="",
            owner=ctx.owner, session_id=ctx.session_id, reference_store=pipe.reference_store, state_store=store)
        for message in projected:
            value = json.loads(message.content)
            assert value["reference_unavailable"] is True
            assert handle not in message.content
        assert json.loads(projected[1].content)["data"]["items"] == "旧已读事实"
        saved = await store.load(owner=ctx.owner, session_id=ctx.session_id)
        assert saved.conversation[0]["content"] == user.content
        assert saved.conversation[1]["content"] == page.content
    asyncio.run(run())
