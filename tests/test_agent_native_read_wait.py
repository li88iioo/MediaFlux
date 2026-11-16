"""默认模型入口经真实 bwrap/SDK 额度拒绝后，只恢复被冻结的读取。"""
from __future__ import annotations

import asyncio
import json
import threading
from copy import deepcopy
from dataclasses import replace

import pytest

from app import config, sdk
from app.agent import rate_limit
from app.agent.feature_gate import initialize_runtime_fence
from app.agent.kernel.bootstrap import _capture_creation_binding, build_agent_kernel_runtime
from app.agent.kernel.model import ModelEvent, ModelEventType, ModelToolCall
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel import sandbox
from app.agent.kernel.sandbox import _workspace_location
from app.agent.kernel.state_types import AgentInput
from app.agent.owner_routes import web_kernel_owner
from tests import test_agent_generation_profiles as profiles
from tests.test_agent_sdk_effects import native_library_backend as native_library_backend

local_config = profiles.local_config
no_external_io = profiles.no_external_io
GOAL = "刷新动漫媒体库，然后检查QB和TMDB配置；保留回执，额度不足时稍后继续。"
SESSION = "native-sdk-read-wait-2026"
OWNER = web_kernel_owner("native-wait-audit")
TASK = {"root_goal": GOAL, "source": GOAL, "constraints": ["只刷新一次媒体库"], "obligations": [
    {"id": item, "description": "检查" + item, "required": True,
     "acceptance": {"kind": "observed_component", "component": item}} for item in ("qbittorrent", "tmdb")]}


@pytest.fixture(autouse=True)
def isolated_workspace(tmp_path, monkeypatch):
    paths = replace(sandbox.get_runtime_paths(), data_dir=tmp_path / "workspace-data")
    monkeypatch.setattr(sandbox, "get_runtime_paths", lambda: paths)


class NativeModel:
    def __init__(self, scripts=()):
        self.scripts = list(scripts)
        self.requests = []

    async def stream(self, request, *, cancellation):
        self.requests.append(request)
        if self.scripts:
            yield ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall(
                "native-" + str(len(self.requests)), "agent__run_code", {"code": self.scripts.pop(0)}))
            yield ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")
        else:
            yield ModelEvent(ModelEventType.TEXT_DELTA, text="已保存这次配置读取结果。")
            yield ModelEvent(ModelEventType.FINISH, finish_reason="stop")


def runtime(model, clock):
    result = build_agent_kernel_runtime(model=model, store=SQLiteKernelStore(clock=lambda: clock[0]))
    result.session.continuation_binding = _capture_creation_binding()
    assert result.session.model_tools == {"agent.run_code"}
    return result


def consume(owner, count):
    assert all(rate_limit.consume_agent_tool(owner, "diagnostics.component").allowed for _ in range(count))


async def park(clock, *, channel="web", preceding_write=False):
    owner = OWNER if channel == "web" else "tg:v1:-1001\x1f7"
    route = {} if channel == "web" else {"chat_id": "-1001", "message_thread_id": 9}
    consume(owner, 11)
    code = ("from pathlib import Path\n"
            "counter=Path('/work/prefix-count')\n"
            "counter.write_text(str(int(counter.read_text())+1) if counter.exists() else '1')\n"
            "print('prefix preserved', flush=True)\n"
            + ("print(mf.library.refresh(server='jellyfin', library_id='anime'), flush=True)\n" if preceding_write else "")
            + "print(mf.diagnostics.component('qbittorrent'), flush=True)\n"
              "print(mf.diagnostics.component('tmdb'), flush=True)\n"
              "Path('/work/tail-ran').write_text('must not be replayed')\n")
    model = NativeModel(["print(mf.session.start_task(**" + repr(TASK) + "))", code])
    active = runtime(model, clock)
    events = [event async for event in active.session.run(AgentInput(
        message=GOAL, owner=owner, session_id=SESSION, request_id="native-source", channel=channel, delivery_route=route))]
    assert events[-1].payload.get("status") == "waiting", [(e.type.value, dict(e.payload)) for e in events if "tool" in e.type.value or "turn" in e.type.value]
    assert len(model.requests) == 2
    cold = await active.store.load(owner=owner, session_id=SESSION)
    record = cold.metadata["read_wait"]
    assert record["state"] == "pending"
    assert record["anchor"]["tool"] == "agent.run_code"
    assert record["sdk_read"]["method"] == "diagnostics.component"
    assert record["sdk_read"]["arguments"] == {"component": "tmdb"}
    assert record["budget"]["model_spent"] == record["budget"]["tool_spent"] == 2
    assert record["budget"]["wait_segments"] == 1
    assert record["due_at"] > clock[0]
    receipt = json.loads(next(row["content"] for row in cold.conversation if row.get("tool_call_id") == "native-2"))
    assert receipt["status"] == "rate_limited" and receipt["ok"] is False
    assert "prefix preserved" in receipt["data"]["stdout"]
    root, _ = _workspace_location(owner, SESSION)
    assert (root / "work/prefix-count").read_text() == "1"
    assert not (root / "work/tail-ran").exists()
    for stream in ("stdout", "stderr"):
        assert (root / receipt["data"][stream + "_path"].lstrip("/")).is_file()
    assert [fact["component"] for fact in cold.metadata["task"]["verified_facts"]] == ["qbittorrent"]
    assert all(item["state"] == "pending" for item in cold.metadata["task"]["obligations"])
    if preceding_write:
        assert record["settled_publication"]["confirmed"] == cold.metadata["confirmed_publication"]
        written = next(call for call in receipt["data"]["sdk_calls"] if call["method"] == "library.refresh")
        saved = json.loads((root / written["response_path"].lstrip("/")).read_text())
        assert saved["result"]["status"] == "accepted"
    return cold, root


@pytest.mark.parametrize("channel", ["web", "telegram"])
def test_native_quota_cold_resume_only_denied_read(local_config, native_library_backend, monkeypatch, channel):
    clock = [100]
    monkeypatch.setattr(rate_limit.agent_rate_limiter, "_wall_clock", lambda: clock[0])
    initialize_runtime_fence()
    config.set_and_save({"TG_CHAT_ID": "-1001", "TG_AGENT_ALLOWED_USER_IDS": "7", "TG_AGENT_ENABLED": "1"})
    _, client = native_library_backend

    async def run():
        parked, root = await park(clock, channel=channel, preceding_write=True)
        original = deepcopy(parked.conversation)
        candidate = parked.metadata["read_wait"]
        clock[0] = candidate["due_at"]
        model = NativeModel()
        fresh = runtime(model, clock)
        assert await fresh.session.resume_wait(candidate=candidate)
        state = await fresh.store.load(owner=parked.owner, session_id=SESSION)
        assert state.conversation[:len(original)] == original
        calls = [call for row in state.conversation[len(original):] for call in row.get("tool_calls", ())]
        assert len(calls) == 1
        assert calls[0]["call_id"] != "native-2"
        assert "prefix-count" not in calls[0]["arguments"]["code"]
        assert "tail-ran" not in calls[0]["arguments"]["code"]
        assert "library.refresh" not in calls[0]["arguments"]["code"]
        assert (root / "work/prefix-count").read_text() == "1"
        assert not (root / "work/tail-ran").exists()
        assert len(model.requests) == 1
        results = [json.loads(row.content) for row in model.requests[0].messages if row.role == "tool"]
        assert results[-1]["data"]["sdk_calls"][0]["method"] == "diagnostics.component"
        assert len(results[-1]["data"]["sdk_calls"]) == 1
        assert state.metadata["read_wait"]["state"] == "terminal"
        assert state.metadata["read_wait"]["terminal_reason"] == "completed"
        assert state.metadata["read_wait"]["budget"]["tool_spent"] == 3
        assert state.metadata["read_wait"]["budget"]["model_spent"] == 3
        assert state.metadata["read_wait"]["budget"]["wait_seconds_spent"] == clock[0] - 100
        assert {fact["component"] for fact in state.metadata["task"]["verified_facts"]} == {"qbittorrent", "tmdb"}
        assert state.metadata["task"]["status"] == "partial", "读取完成不代替模型关联清单与证据"
        assert not await fresh.session.resume_wait(candidate=candidate)
        if channel == "telegram":
            deliveries = list(state.metadata["continuation_deliveries"].values())
            assert len(deliveries) == 1
            assert deliveries[0]["delivery_route"] == {"chat_id": "-1001", "message_thread_id": 9}
            assert "配置读取结果" in deliveries[0]["delivery_view"]["answer"]
    asyncio.run(run())
    client.refresh_library.assert_called_once_with("anime", mode="scan", replace_images=False, regenerate_trickplay=False)


@pytest.mark.parametrize("entry", ["cancel", "runtime_changed", "method_changed", "new_user", "expired"])
def test_native_wait_revoked_before_cold_resume(local_config, monkeypatch, entry):
    clock = [100]
    monkeypatch.setattr(rate_limit.agent_rate_limiter, "_wall_clock", lambda: clock[0])

    async def run():
        state, root = await park(clock)
        candidate = state.metadata["read_wait"]
        clock[0] = candidate["due_at"]
        model = NativeModel()
        fresh = runtime(model, clock)
        if entry == "cancel":
            assert await fresh.session.cancel(owner=OWNER, session_id=SESSION, request_id="native-source")
        elif entry == "runtime_changed":
            config.set_and_save({"AGENT_LLM_MODEL": "changed-route"})
            fresh.session.continuation_binding = _capture_creation_binding()
        elif entry == "method_changed":
            monkeypatch.setattr(sdk, "METHOD_EFFECTS", {**sdk.METHOD_EFFECTS, "diagnostics.component": "write"})
        elif entry == "new_user":
            await fresh.store.begin_turn(owner=OWNER, session_id=SESSION, request_id="new-user")
        else:
            clock[0] = candidate["expires_at"]
        assert not await fresh.session.resume_wait(candidate=candidate)
        assert not model.requests
        assert (root / "work/prefix-count").read_text() == "1"
        assert not (root / "work/tail-ran").exists()
        saved = await fresh.store.load(owner=OWNER, session_id=SESSION)
        assert saved.metadata["read_wait"]["state"] in {"terminal", "cancelled"}
        assert [fact["component"] for fact in saved.metadata["task"]["verified_facts"]] == ["qbittorrent"]
    asyncio.run(run())


def test_native_wait_reparks_same_read_with_cumulative_budget(local_config, monkeypatch):
    clock = [100]
    monkeypatch.setattr(rate_limit.agent_rate_limiter, "_wall_clock", lambda: clock[0])

    async def run():
        state, root = await park(clock)
        previous = state.metadata["read_wait"]
        for segment in (2, 3):
            clock[0] = previous["due_at"]
            while rate_limit.consume_agent_tool(OWNER, "diagnostics.component").allowed:
                pass
            model = NativeModel()
            fresh = runtime(model, clock)
            assert await fresh.session.resume_wait(candidate=previous)
            saved = await fresh.store.load(owner=OWNER, session_id=SESSION)
            current = saved.metadata["read_wait"]
            assert current["state"] == "pending"
            assert current["wait_id"] == previous["wait_id"]
            assert current["expires_at"] == previous["expires_at"]
            assert current["sdk_read"]["arguments"] == previous["sdk_read"]["arguments"]
            assert current["sdk_read"]["rpc_id"] != previous["sdk_read"]["rpc_id"]
            assert current["budget"]["wait_segments"] == segment
            assert current["budget"]["tool_spent"] == segment + 1
            assert current["budget"]["model_spent"] == 2
            assert not model.requests
            previous = current
        clock[0] = previous["due_at"]
        while rate_limit.consume_agent_tool(OWNER, "diagnostics.component").allowed:
            pass
        final_model = NativeModel()
        final = runtime(final_model, clock)
        assert await final.session.resume_wait(candidate=previous)
        saved = await final.store.load(owner=OWNER, session_id=SESSION)
        assert saved.metadata["read_wait"]["state"] == "terminal"
        assert saved.metadata["read_wait"]["terminal_reason"] == "rate_limited"
        assert saved.metadata["read_wait"]["budget"]["wait_segments"] == 3
        assert saved.metadata["read_wait"]["budget"]["tool_spent"] == 5
        assert not final_model.requests
        assert (root / "work/prefix-count").read_text() == "1"
        assert not (root / "work/tail-ran").exists()
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["exception", "backend_quota", "false_result"])
def test_started_native_read_failure_never_reparks_or_calls_model(local_config, monkeypatch, failure):
    from app.agent.errors import ToolRateLimitError
    from app.agent.rate_limit import RateLimitDecision
    clock = [100]
    monkeypatch.setattr(rate_limit.agent_rate_limiter, "_wall_clock", lambda: clock[0])

    async def run():
        state, root = await park(clock)
        candidate = state.metadata["read_wait"]
        clock[0] = candidate["due_at"]
        calls = []

        def read(component):
            calls.append(component)
            if failure == "exception":
                raise ValueError("offline backend read failed")
            if failure == "backend_quota":
                raise ToolRateLimitError(RateLimitDecision(False, 12, 1, 0, 10, clock[0] + 10, "quota"))
            return {"ok": False, "status": "failed", "summary": "offline negative read"}

        monkeypatch.setitem(sandbox.METHODS, "diagnostics.component", read)
        model = NativeModel()
        fresh = runtime(model, clock)
        assert await fresh.session.resume_wait(candidate=candidate)
        saved = await fresh.store.load(owner=OWNER, session_id=SESSION)
        assert calls == ["tmdb"]
        assert not model.requests
        assert saved.metadata["read_wait"]["state"] == "terminal"
        assert saved.metadata["read_wait"]["budget"]["wait_segments"] == 1
        assert [fact["component"] for fact in saved.metadata["task"]["verified_facts"]] == ["qbittorrent"]
        receipt = json.loads([row["content"] for row in saved.conversation if row.get("role") == "tool"][-1])
        assert receipt["ok"] is False
        assert receipt["data"]["sdk_calls"][0]["ok"] is False
        assert (root / "work/prefix-count").read_text() == "1"
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["different_query", "same_query", "same_query_retry"])
def test_parallel_native_quota_reports_partial_without_discarding_other_rpc(local_config, monkeypatch, mode):
    clock = [100]
    monkeypatch.setattr(rate_limit.agent_rate_limiter, "_wall_clock", lambda: clock[0])
    entered, release = threading.Event(), threading.Event()
    original = sandbox.METHODS["diagnostics.component"]
    backend_calls = []

    def read(component):
        assert component == "qbittorrent", "被拒绝的读取不能进入后端"
        backend_calls.append(component)
        entered.set()
        assert release.wait(5)
        return original(component)

    monkeypatch.setitem(sandbox.METHODS, "diagnostics.component", read)
    query = "tmdb" if mode == "different_query" else "qbittorrent"
    code = ("import json, time\nfrom concurrent.futures import ThreadPoolExecutor\n"
            "with ThreadPoolExecutor(2) as pool:\n"
            "    first = pool.submit(mf.diagnostics.component, 'qbittorrent')\n"
            "    time.sleep(.15)\n"
            "    try:\n"
            f"        mf.diagnostics.component({query!r})\n"
            "    except mf.Error as error:\n"
            "        print(json.dumps({'code': error.code, 'execution_started': error.execution_started}))\n"
            "    print(json.dumps(first.result()))\n"
            + ("print(json.dumps(mf.diagnostics.component('qbittorrent')))\n" if mode == "same_query_retry" else ""))

    async def run():
        consume(OWNER, 11)
        model = NativeModel(["mf.session.start_task(**" + repr(TASK) + ")", code])
        active = runtime(model, clock)
        events = []
        try:
            async with asyncio.timeout(10):
                async for event in active.session.run(AgentInput(message=GOAL, owner=OWNER,
                        session_id=SESSION, request_id="parallel-source", channel="web")):
                    events.append(event)
                    activity = event.payload.get("activity") or {}
                    if activity.get("status") == "failed":
                        assert entered.is_set()
                        if mode == "same_query_retry":
                            clock[0] = 200
                        release.set()
        finally:
            release.set()
        expected = "success" if mode == "same_query_retry" else "partial"
        assert events[-1].payload["status"] == expected
        assert not any(event.payload.get("status") == "waiting" for event in events)
        saved = await active.store.load(owner=OWNER, session_id=SESSION)
        assert saved.metadata["read_wait"]["state"] == "idle"
        assert saved.metadata["task"]["status"] == "partial"
        assert [fact["component"] for fact in saved.metadata["task"]["verified_facts"]] == ["qbittorrent"]
        result = json.loads(next(row["content"] for row in saved.conversation if row.get("tool_call_id") == "native-2"))
        assert result["status"] == expected
        calls = result["data"]["sdk_calls"]
        assert len(calls) == (3 if mode == "same_query_retry" else 2)
        assert len(backend_calls) == (2 if mode == "same_query_retry" else 1)
        refused = next(call for call in calls if call["ok"] is False)
        assert refused["status"] == "rate_limited" and refused["execution_started"] is False
        assert '"code": "rate_limited", "execution_started": false' in result["data"]["stdout"]
        completed = next(call for call in calls if call["ok"] is True)
        root, _ = _workspace_location(OWNER, SESSION)
        archived = json.loads((root / completed["response_path"].lstrip("/")).read_text())
        assert archived["result"]["data"]["component"] == "qbittorrent"
        assert archived["result"]["ok"] is True
        assert await active.store.due_continuations() == []
    asyncio.run(run())


def test_native_read_only_resume_scope_ends_before_new_model_write(local_config, native_library_backend, monkeypatch):
    clock = [100]
    monkeypatch.setattr(rate_limit.agent_rate_limiter, "_wall_clock", lambda: clock[0])
    initialize_runtime_fence()
    _, client = native_library_backend

    async def run():
        parked, _ = await park(clock)
        candidate = parked.metadata["read_wait"]
        clock[0] = candidate["due_at"]
        model = NativeModel(["print(mf.library.refresh(server='jellyfin', library_id='anime'))"])
        fresh = runtime(model, clock)
        assert await fresh.session.resume_wait(candidate=candidate)
        state = await fresh.store.load(owner=OWNER, session_id=SESSION)
        assert len(model.requests) == 2
        receipt = json.loads([row["content"] for row in state.conversation if row.get("role") == "tool"][-1])
        assert receipt["data"]["sdk_calls"][0]["method"] == "library.refresh"
        assert receipt["data"]["sdk_calls"][0]["ok"] is True
        assert state.metadata["read_wait"]["state"] == "terminal"
    asyncio.run(run())
    client.refresh_library.assert_called_once()


def test_unrestorable_large_sdk_arguments_keep_catchable_error_without_parking(local_config, monkeypatch):
    clock = [100]
    monkeypatch.setattr(rate_limit.agent_rate_limiter, "_wall_clock", lambda: clock[0])

    async def run():
        assert all(rate_limit.consume_agent_tool(OWNER, "diagnostics.search").allowed for _ in range(30))
        model = NativeModel(["mf.session.start_task(**" + repr(TASK) + ")",
            "try:\n    mf.diagnostics.search(query='影'*65000, sections=['downloads'])\n"
            "except mf.Error as error:\n    print(error.code, error.execution_started)\n"])
        active = runtime(model, clock)
        events = [event async for event in active.session.run(AgentInput(message=GOAL, owner=OWNER,
            session_id=SESSION, request_id="large-source", channel="web"))]
        assert events[-1].payload["status"] == "partial"
        state = await active.store.load(owner=OWNER, session_id=SESSION)
        assert state.metadata["read_wait"]["state"] == "idle"
        result = json.loads(next(row["content"] for row in state.conversation if row.get("tool_call_id") == "native-2"))
        assert result["status"] == "partial"
        assert "rate_limited False" in result["data"]["stdout"]
        assert not state.metadata["task"]["verified_facts"]
        assert await active.store.due_continuations() == []
    asyncio.run(run())
