"""Explicit task lifecycle through real native wire, domain tools and SQLite."""
from tests.agent_task_native_helpers import task_call
from typing import cast

import asyncio
import json
from tests.agent_task_native_helpers import returned_task
import socket
from contextlib import contextmanager
from dataclasses import asdict, replace
from unittest.mock import patch

import httpx
import pytest
from starlette.requests import Request

from app import database as db
from app.agent.kernel.adapters import TurnViewBuilder
from app.agent.kernel.bootstrap import build_agent_kernel_runtime
from app.agent.kernel.conversation_state import retain_conversation
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.provider_model import (
    OpenAICompatibleModelAdapter,
    ProviderSettings,
)
from app.agent.kernel.state_types import AgentInput
from app.agent.owner_routes import web_kernel_owner
from app.clients.http import FixedHostHttpClient
from app.routes import agent_api

GOAL = "观察 TMDB 和 qBittorrent 当前配置状态，仅观察、不修改。"
OWNER = web_kernel_owner("admin")


def declaration():
    return {
        "operation": "create",
        "source": GOAL,
        "root_goal": GOAL,
        "constraints": ["仅观察、不修改"],
        "obligations": [
            {
                "id": component,
                "description": "观察配置 " + component,
                "required": True,
                "acceptance": {"kind": "observed_component", "component": component},
            }
            for component in ("tmdb", "qbittorrent")
        ],
    }


def configuration_call(method, arguments):
    code = "import json\nfrom mf import diagnostics\nprint(json.dumps(diagnostics." + method.split(".")[1] + "(**" + repr(arguments) + "), ensure_ascii=False))"
    return "agent.run_code", {"code": code}


class NativeScript:
    def __init__(self, component, create=False, *, observation_tool="diagnostics.component"):
        self.component = component
        self.observation_tool = observation_tool
        self.steps = (
            ["discover"]
            + (["create"] if create else [])
            + ["observe", "checkpoint", "finish"]
        )
        self.requests = []
        self.model_requests = []
        self.latest = None
        self.force_final_tool = False
        script = self

        class Adapter(OpenAICompatibleModelAdapter):
            async def stream(self, request, *, cancellation):
                script.model_requests.append(asdict(request))
                async for event in super().stream(request, cancellation=cancellation):
                    yield event

        self.adapter = Adapter(
            ProviderSettings(
                api_url="https://api.example.com/v1",
                model="fixture",
                protocol="chat_completions",
            ),
            client_factory=lambda **kw: FixedHostHttpClient(
                **kw,
                transport=httpx.MockTransport(self.respond),
                resolver=lambda host, port: [
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))
                ],
            ),
        )

    def assert_user_inputs(self, expected_users):
        """原话必须完整；额外 wire 输入只能逐项对应本次请求的真实运行时回执。"""
        assert len(self.requests) == len(self.model_requests)
        for request, wire in zip(self.model_requests, self.requests, strict=True):
            messages = request["messages"]
            assert [row["content"] for row in messages if row["role"] == "user"] == expected_users
            inputs = []
            for row in messages:
                if row.get("completion_receipt_id"):
                    assert row["role"] == "assistant" and not row.get("tool_calls")
                    inputs.append("运行时执行结果通知（不是用户新指令或新增写入授权）：\n" + row["content"])
                elif row["role"] == "user":
                    inputs.append(row["content"])
            assert [row["content"] for row in wire["messages"] if row["role"] == "user"] == inputs

    async def respond(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        for message in body["messages"]:
            if message.get("role") == "tool":
                try:
                    value = json.loads(message["content"])
                except (ValueError, TypeError):
                    continue
                if task := returned_task(value):
                    self.latest = task
        step = self.steps.pop(0)
        if step == "finish" and not self.force_final_tool:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"content": "全部任务完成。"},
                            "finish_reason": "stop",
                        }
                    ]
                },
            )
        if step == "discover":
            name, args = "agent__run_code", {"language": "shell", "code": "mf --help"}
        elif step == "create":
            name, args = task_call(declaration(), wire=True)
        elif step == "observe":
            tool, args = configuration_call(self.observation_tool, {"component": self.component} if self.component is not None else {})
            name = tool.replace(".", "__")
        else:
            task = self.latest or {}
            name, args = task_call({
                    "operation": "checkpoint",
                    "evidence_ids": [
                        f["evidence_id"]
                        for f in task.get("verified_facts", [])
                        if self.component is None or f["component"] == self.component
                    ],
                    "blockers": [],
                }, wire=True)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call-" + step,
                                    "type": "function",
                                    "function": {
                                        "name": name,
                                        "arguments": json.dumps(
                                            args, ensure_ascii=False
                                        ),
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        )


@pytest.mark.parametrize("budget", [False, True])
def test_observe_checkpoint_prune_reopen_continue(tmp_path, budget):
    previous = db.DB_PATH, bool(getattr(db, "_configured_test_mode", False))
    db.configure_database(tmp_path / "task.db", test_mode=True)
    db.init_db()

    async def run():
        store = SQLiteKernelStore(secret_provider=lambda: "task-test-only")
        first = NativeScript("tmdb", create=True)
        runtime = build_agent_kernel_runtime(model=first.adapter, store=store)
        events = [
            e
            async for e in runtime.session.run(
                AgentInput(message=GOAL, owner=OWNER, session_id="task-session-0001")
            )
        ]
        (tmp_path / "first-events.json").write_text(
            json.dumps(
                [{"type": e.type.value, "payload": dict(e.payload)} for e in events],
                ensure_ascii=False,
                indent=2,
            )
        )
        assert {tool["function"]["name"] for tool in first.requests[0]["tools"]} == {"agent__run_code"}
        [help_result] = [event.payload["result"]["data"] for event in events
                         if event.type.value == "tool.completed" and event.payload.get("call_id") == "call-discover"]
        assert help_result["exit_code"] == 0
        assert "session" in help_result["stdout"] and "diagnostics" in help_result["stdout"]
        assert help_result["sdk_calls"] == []
        cold = await store.load(owner=OWNER, session_id="task-session-0001")
        assert cold.metadata["task"]["status"] == "partial"
        assert events[-1].payload["task"]["status"] == "partial"
        assert cold.metadata["task"]["obligations"][0]["state"] == "verified"
        retained = retain_conversation(
            cold.conversation + [{"role": "user", "content": "保留的新回合"}],
            maximum_bytes=100,
        )
        assert GOAL not in json.dumps(retained, ensure_ascii=False)
        lease, _ = await store.begin_turn(
            owner=OWNER, session_id="task-session-0001", request_id="prune"
        )
        await store.commit(lease, conversation=retained)
        fresh = SQLiteKernelStore(secret_provider=lambda: "task-test-only")
        second = NativeScript("qbittorrent")
        runtime2 = build_agent_kernel_runtime(model=second.adapter, store=fresh)
        if budget:
            second.force_final_tool = True
            runtime2.session.limits = replace(runtime2.session.limits, max_tool_calls=3)
        events2 = [
            e
            async for e in runtime2.session.run(
                AgentInput(message="继续", owner=OWNER, session_id="task-session-0001")
            )
        ]
        for projection in (second.model_requests[0], second.requests[0]):
            text = json.dumps(projection, ensure_ascii=False)
            assert GOAL in text and "仅观察、不修改" in text
            assert "verified_facts" in text and "tmdb" in text and "qbittorrent" in text
        assert (
            sum(
                any(c["method"] == "diagnostics.component" for c in e.payload.get("result", {}).get("data", {}).get("sdk_calls", []))
                for e in events2
                if e.type.value == "tool.completed"
            )
            == 1
        )
        end = await SQLiteKernelStore(secret_provider=lambda: "task-test-only").load(
            owner=OWNER, session_id="task-session-0001"
        )
        assert end.metadata["task"]["status"] == "complete"
        assert events2[-1].payload["turn_status"] == events2[-1].payload["status"]
        assert events2[-1].payload["task"]["status"] == "complete"
        assert events2[-1].payload["turn_status"] == (
            "partial" if budget else "success"
        )
        builder = TurnViewBuilder()
        for event in events2:
            builder.apply(event)
        assert builder.build().to_dict()["task"] == events2[-1].payload["task"]
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/agent/sessions/task-session-0001",
                "headers": [],
                "session": {"logged_in": True},
            }
        )
        with patch.object(agent_api, "get_agent_kernel_runtime", return_value=runtime2):
            response = await agent_api.get_session(request, "task-session-0001")
        assert response.status_code == 200
        api = json.loads(cast(bytearray | bytes | str, response.body))
        assert api["task"] == events2[-1].payload["task"]
        assert api["turn_status"] == events2[-1].payload["turn_status"]
        (tmp_path / "vertical.json").write_text(
            json.dumps(
                {
                    "first_requests": first.model_requests,
                    "first_wire": first.requests,
                    "first_terminal": dict(events[-1].payload),
                    "retained": retained,
                    "second_requests": second.model_requests,
                    "second_wire": second.requests,
                    "second_events": [
                        {"type": e.type.value, "payload": dict(e.payload)}
                        for e in events2
                    ],
                    "cold_state": end.to_payload(),
                    "api": api,
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    try:
        asyncio.run(run())
    finally:
        db.configure_database(previous[0], test_mode=previous[1])


def test_no_task_qa_transaction_trace(tmp_path):
    previous = db.DB_PATH, bool(getattr(db, "_configured_test_mode", False))
    db.configure_database(tmp_path / "qa.db", test_mode=True)
    db.init_db()
    trace = []
    original = db.get_conn

    @contextmanager
    def traced():
        with original() as connection:
            connection.set_trace_callback(trace.append)
            yield connection

    async def run():
        model = NativeScript("tmdb")
        model.steps = ["finish"]
        store = SQLiteKernelStore(secret_provider=lambda: "task-test-only")
        runtime = build_agent_kernel_runtime(model=model.adapter, store=store)
        with patch.object(db, "get_conn", traced):
            events = [
                e
                async for e in runtime.session.run(
                    AgentInput(
                        message="你好", owner=OWNER, session_id="qa-session-000001"
                    )
                )
            ]
        assert len(model.requests) == 1
        assert not any(e.type.value == "tool.started" for e in events)
        assert events[-1].payload["status"] == "success"
        assert events[-1].payload.get("task") is None
        assert (
            "task"
            not in (
                await store.load(owner=OWNER, session_id="qa-session-000001")
            ).metadata
        )
        (tmp_path / "qa-trace.json").write_text(
            json.dumps(
                {
                    "transactions": [
                        s
                        for s in trace
                        if s.startswith(("BEGIN", "COMMIT", "ROLLBACK"))
                    ],
                    "model_calls": len(model.requests),
                    "tools": 0,
                },
                indent=2,
            )
        )

    try:
        asyncio.run(run())
    finally:
        db.configure_database(previous[0], test_mode=previous[1])


@pytest.mark.parametrize("failure", ["invalid", "exception", "commit"])
def test_native_failed_observation_is_atomic_and_object_scoped(tmp_path, failure):
    from app.modules import diagnostics as domain

    previous = db.DB_PATH, bool(getattr(db, "_configured_test_mode", False))
    db.configure_database(tmp_path / "failure.db", test_mode=True)
    db.init_db()
    original = domain.component_configuration

    class FailingStore(SQLiteKernelStore):
        async def commit(
            self, lease, *, conversation=None, updates=(), task_patch=None
        ):
            if (
                failure == "commit"
                and task_patch is not None
                and task_patch.operation == "observe"
            ):
                raise ValueError("injected completion capacity failure")
            return await super().commit(
                lease,
                conversation=conversation,
                updates=updates,
                **({"task_patch": task_patch} if task_patch is not None else {}),
            )

    def broken(component):
        if component == "tmdb":
            if failure == "exception":
                raise RuntimeError("synthetic query failure")
            if failure == "invalid":
                return {**original(component), "network_accessed": True}
        return original(component)

    async def turn(store, component, create=False, break_query=False):
        script = NativeScript(component, create)
        runtime = build_agent_kernel_runtime(model=script.adapter, store=store)
        events = []
        with patch.object(
            domain, "component_configuration", broken if break_query else original
        ):
            async for event in runtime.session.run(
                AgentInput(
                    message=GOAL if create else "继续",
                    owner=OWNER,
                    session_id="failure-session-01",
                )
            ):
                events.append(event)
                if (
                    break_query and failure != "commit"
                    and event.type.value == "tool.completed"
                    and event.payload.get("call_id") == "call-observe"
                ):
                    cold = await store.load(
                        owner=OWNER, session_id="failure-session-01"
                    )
                    assert not cold.metadata["task"]["verified_facts"]
                    assert any(
                        row.get("role") == "tool"
                        and row.get("tool_call_id") == "call-observe"
                        for row in cold.conversation
                    )
        return events, await store.load(owner=OWNER, session_id="failure-session-01")

    async def run():
        store = FailingStore(secret_provider=lambda: "task-test-only")
        events, first = await turn(store, "tmdb", True, True)
        assert not first.metadata["task"]["verified_facts"]
        completed = [e.payload["result"]["data"] for e in events
                     if e.type.value == "tool.completed" and any(c["method"] == "diagnostics.component" for c in e.payload.get("result", {}).get("data", {}).get("sdk_calls", []))]
        if failure == "commit":
            assert not completed  # 未保存的结果仍不可发布。
        else:
            assert len(completed) == 1
            assert (completed[0]["exit_code"] != 0) == (failure == "exception")
        assert first.metadata["task"]["status"] != "complete"
        if failure == "commit":
            assert "observed_at" not in json.dumps(first.conversation)
            return
        query_blocker = {
            "origin": "query", "component": "tmdb", "epoch": 1, "text": "query:tmdb"
        }
        assert first.metadata["task"]["blockers"] == [query_blocker]
        assert events[-1].payload["task"]["status"] == "blocked"
        _, other = await turn(store, "qbittorrent")
        assert other.metadata["task"]["blockers"] == [query_blocker]
        _, recovered = await turn(store, "tmdb")
        assert recovered.metadata["task"]["blockers"] == []
        assert recovered.metadata["task"]["status"] == "complete"

    try:
        asyncio.run(run())
    finally:
        db.configure_database(previous[0], test_mode=previous[1])



@pytest.mark.parametrize("checkpoint", [False, True])
def test_native_snapshot_capacity_failure_and_cold_recovery(tmp_path, checkpoint):
    from app.modules import diagnostics as domain
    from app.agent.rate_limit import AgentRateLimiter

    previous = db.DB_PATH, bool(getattr(db, "_configured_test_mode", False))
    db.configure_database(tmp_path / "snapshots.db", test_mode=True)
    db.init_db()
    original = domain.component_configuration
    session_id = "snapshot-recovery"
    clock = [0.0]

    def broken(component):
        if component == "qbittorrent":
            raise RuntimeError("synthetic query failure")
        return original(component)

    async def turn(component, *, create=False, accept=False, fail=False):
        clock[0] += 3600  # 各回合跨过限流窗口，不扩大任何配额。
        script = NativeScript(component, create)
        script.steps = ["discover"] + (["create"] if create else []) + ["observe"] + (["checkpoint"] if accept else []) + ["finish"]
        store = SQLiteKernelStore(secret_provider=lambda: "task-test-only")
        runtime = build_agent_kernel_runtime(model=script.adapter, store=store)
        with patch.object(domain, "component_configuration", broken if fail else original):
            events = [event async for event in runtime.session.run(AgentInput(
                message=GOAL if create else "继续", owner=OWNER, session_id=session_id,
            ))]
        cold = await SQLiteKernelStore(secret_provider=lambda: "task-test-only").load(owner=OWNER, session_id=session_id)
        failures = [event for event in events if event.type.value == "tool.failed"]
        assert not failures
        if fail:
            completed = [event.payload["result"]["data"] for event in events
                         if event.type.value == "tool.completed"
                         and event.payload.get("tool") in {"agent.run_code", "agent__run_code"}
                         and any(call["method"] == "diagnostics.component"
                                 for call in event.payload["result"]["data"]["sdk_calls"])]
            assert len(completed) == 1 and completed[0]["exit_code"] != 0
            assert "synthetic query failure" in completed[0]["stderr"]
            assert completed[0]["sdk_calls"][0]["ok"] is False
        return cold.metadata["task"]

    async def run():
        for index in range(2):
            task = await turn("tmdb", create=index == 0, accept=checkpoint)
            assert len(task["verified_facts"]) == 1
            assert task["status"] == "partial"
            assert task["obligations"][0]["state"] == ("verified" if checkpoint else "pending")
            assert task["obligations"][0]["evidence_ids"] == ([task["verified_facts"][0]["evidence_id"]] if checkpoint else [])
            assert task["obligations"][1]["state"] == "pending"
        if not checkpoint:
            task = await turn("tmdb", accept=True)
        accepted = task["verified_facts"][0]
        task = await turn("qbittorrent", fail=True)
        assert task["status"] == "blocked" and task["verified_facts"] == [accepted]
        assert task["blockers"] == [{"origin": "query", "component": "qbittorrent", "epoch": task["epoch"], "text": "query:qbittorrent"}]
        task = await turn("qbittorrent")
        assert task["blockers"] == [] and task["status"] == "partial"
        assert task["verified_facts"][0] == accepted
        assert task["obligations"][1]["state"] == "pending" and task["obligations"][1]["evidence_ids"] == []
        task = await turn("qbittorrent", accept=True)
        assert task["status"] == "complete" and task["blockers"] == []
        assert task["verified_facts"][0] == accepted and len(task["verified_facts"]) == 2
        assert [o["evidence_ids"] for o in task["obligations"]] == [[f["evidence_id"]] for f in task["verified_facts"]]

    try:
        with patch("app.agent.rate_limit.agent_rate_limiter", AgentRateLimiter(clock=lambda: clock[0], shared=False)):
            asyncio.run(run())
    finally:
        db.configure_database(previous[0], test_mode=previous[1])



@pytest.mark.parametrize("qb_state", ["ready", "incomplete", "invalid", "error"])
def test_native_diagnose_batch_attention_to_explicit_checkpoint_and_sqlite(tmp_path, qb_state):
    from app.modules import diagnostics as diagnosis
    from app.modules import diagnostics as explain

    previous = db.DB_PATH, bool(getattr(db, "_configured_test_mode", False))
    db.configure_database(tmp_path / "diagnose-task.db", test_mode=True)
    db.init_db()
    # 全是隔离夹具值；Jellyfin 缺项令总体 attention，不能抹掉 TMDB/qB 的配置观察。
    values = {"TMDB_API_KEY": "fixture-tmdb", "QB_URL": "http://qb.invalid", "JELLYFIN_ENABLED": "1"}
    if qb_state != "incomplete":
        values["QB_API_KEY"] = "fixture-qb"
    original = diagnosis.component_configuration

    def component_configuration(component, **kwargs):
        if component == "qbittorrent" and qb_state == "error":
            raise RuntimeError("synthetic diagnosis failure")
        payload = original(component, **kwargs)
        return {**payload, "enabled": 1} if component == "qbittorrent" and qb_state == "invalid" else payload

    async def run():
        script = NativeScript(None, create=True, observation_tool="diagnostics.configuration")
        if qb_state in {"invalid", "error"}:
            script.steps = ["discover", "create", "observe", "finish"]
        store = SQLiteKernelStore(secret_provider=lambda: "task-test-only")
        runtime = build_agent_kernel_runtime(model=script.adapter, store=store)
        session_id = "diagnose-batch"
        observed = None
        events = []
        with (
            patch.object(diagnosis, "_effective_value", return_value=lambda key: values.get(key, "")),
            patch.object(diagnosis, "component_configuration", side_effect=component_configuration),
            patch.object(explain.config, "has_external_override", return_value=False),
            patch.object(socket.socket, "connect", side_effect=AssertionError("test must remain offline")),
        ):
            if qb_state != "error":
                actual = diagnosis.configuration()
                assert actual["ok"] is False and actual["status"] == "attention"
            async for event in runtime.session.run(AgentInput(message=GOAL, owner=OWNER, session_id=session_id)):
                events.append(event)
                if event.payload.get("call_id") == "call-observe" and event.type.value in {"tool.completed", "tool.failed"}:
                    assert event.type.value == "tool.completed"
                    result = event.payload["result"]["data"]
                    assert (result["exit_code"] != 0) == (qb_state == "error")
                    assert result["sdk_calls"][0]["ok"] is False
                    if qb_state == "error":
                        assert "synthetic diagnosis failure" in result["stderr"]
                    else:
                        assert result["sdk_calls"][0]["status"] == "attention"
                    observed = (await SQLiteKernelStore(secret_provider=lambda: "task-test-only").load(owner=OWNER, session_id=session_id)).metadata["task"]
                    assert observed["revision"] == 2  # create + 一次批量观察。
                    assert all(o["state"] == "pending" and not o["evidence_ids"] for o in observed["obligations"])
        assert observed is not None
        failures = [event for event in events if event.type.value == "tool.failed"]
        assert not failures
        expected = set() if qb_state == "error" else {"tmdb"} if qb_state == "invalid" else {"tmdb", "qbittorrent"}
        assert {fact["component"] for fact in observed["verified_facts"]} == expected
        for fact in observed["verified_facts"]:
            assert fact["tool"] == "diagnostics.configuration" and fact["call_id"] == "call-observe:1"
            assert fact["scope"]["owner"] == OWNER and fact["scope"]["session_id"] == session_id
            assert fact["network_accessed"] is False and fact["probe_mode"] == "configuration_only"
            assert fact["status"] == (qb_state if fact["component"] == "qbittorrent" else "ready")
        cold = await SQLiteKernelStore(secret_provider=lambda: "task-test-only").load(owner=OWNER, session_id=session_id)
        task = cold.metadata["task"]
        accepted = qb_state not in {"invalid", "error"}
        assert task["revision"] == (3 if accepted else 2)  # 只有显式 checkpoint 才关联。
        evidence = {fact["component"]: fact["evidence_id"] for fact in task["verified_facts"]}
        for obligation in task["obligations"]:
            assert obligation["evidence_ids"] == ([evidence[obligation["id"]]] if accepted else [])
        assert task["status"] == ("complete" if accepted else "blocked")
        blocked = {"tmdb", "qbittorrent"} if qb_state == "error" else {"qbittorrent"} if qb_state == "invalid" else set()
        assert {blocker["component"] for blocker in task["blockers"]} == blocked
        assert all(blocker == {"origin": "query", "component": blocker["component"], "epoch": task["epoch"], "text": "query:" + blocker["component"]} for blocker in task["blockers"])
        wire = json.dumps(script.requests, ensure_ascii=False)
        assert all(value not in wire for value in ("fixture-tmdb", "fixture-qb", "qb.invalid"))
        assert events[-1].payload["task"]["status"] == task["status"]

    try:
        asyncio.run(run())
    finally:
        db.configure_database(previous[0], test_mode=previous[1])


def test_caught_diagnostic_error_preserves_script_result_and_task_evidence(tmp_path, monkeypatch):
    from app.modules import diagnostics

    original_call = configuration_call
    original_component = diagnostics.component_configuration

    def broken(component):
        if component == "qbittorrent":
            raise RuntimeError("synthetic unavailable")
        return original_component(component)

    def recover(method, arguments):
        _, first = original_call(method, arguments)
        code = "try:\n" + "\n".join("    " + line for line in first["code"].splitlines())
        code += "\nexcept Exception as exc:\n    print('caught:', exc)"
        code += "\nprint(json.dumps(diagnostics.component('tmdb'), ensure_ascii=False))\nprint('script recovered')"
        return "agent.run_code", {"code": code}

    monkeypatch.setattr(f"{__name__}.configuration_call", recover)
    monkeypatch.setattr(diagnostics, "component_configuration", broken)
    previous = db.DB_PATH, bool(getattr(db, "_configured_test_mode", False))
    db.configure_database(tmp_path / "caught-diagnostic.db", test_mode=True)
    db.init_db()

    async def run():
        script = NativeScript("qbittorrent", create=True)
        script.steps = ["discover", "create", "observe", "finish"]
        store = SQLiteKernelStore(secret_provider=lambda: "task-test-only")
        runtime = build_agent_kernel_runtime(model=script.adapter, store=store)
        events = [event async for event in runtime.session.run(AgentInput(
            message=GOAL, owner=OWNER, session_id="caught-diagnostic"))]
        terminal = [event for event in events if event.payload.get("call_id") == "call-observe"
                    and event.type.value in {"tool.completed", "tool.failed"}]
        assert len(terminal) == 1 and terminal[0].type.value == "tool.completed"
        result = terminal[0].payload["result"]["data"]
        assert result["exit_code"] == 0 and "script recovered" in result["stdout"]
        assert [(call["method"], call["ok"]) for call in result["sdk_calls"]] == [
            ("diagnostics.component", False), ("diagnostics.component", True)]
        saved = await store.load(owner=OWNER, session_id="caught-diagnostic")
        task = saved.metadata["task"]
        assert task["status"] == "blocked"
        assert {fact["component"] for fact in task["verified_facts"]} == {"tmdb"}
        assert {blocker["component"] for blocker in task["blockers"]} == {"qbittorrent"}

    try:
        asyncio.run(run())
    finally:
        db.configure_database(previous[0], test_mode=previous[1])
