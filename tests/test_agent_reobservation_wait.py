"""#18：失败确认的 READ quota 路径与私有来源冷恢复，全部为离线原生回归。

同步失败不凭空制造 read_wait；后台失败通过真实受理/worker/轮询形成 effect
续行。仅控制离线 Provider、额度拒绝与时钟，不手造 wait、lease 或签名。
"""
from __future__ import annotations

import asyncio
import json
import socket
from copy import deepcopy
from dataclasses import replace
from functools import partial
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from app.agent.kernel.tool_calls import PreparedEffect
import requests

from app import database as db
from app.agent import effect_completion as effects
from app.agent.errors import ToolRateLimitError
from app.agent.kernel.bootstrap import _capture_creation_binding
from app.agent.kernel.events import AgentEventType
from app.agent.kernel.ports.mediaflux_policy import MediaFluxToolRateLimiter
from app.agent.kernel.state_types import AgentInput
from app.agent.models import ToolContext
from app.agent.rate_limit import RateLimitDecision
from app.agent.task_stop import stop_agent_session
from app.modules import organize_tasks
from app.repositories import organize_operation_jobs as jobs
from tests.support import initialized_web_runtime
from tests.test_agent_effect_admission import (
    _domain_arguments,
    _offline_runtime,
    _OfflineClient,
    _paused_manager,
    _run_real_worker,
    _spec,
)
from tests.test_agent_failed_effect_followup import GOAL, READ, A, NativeCase
from tests.test_agent_task_first_slice import NativeScript

WRITE = "cloud.change"
PRIVATE = "PRIVATE-REOBSERVATION-WAIT-ENTITY-2734"


@pytest.fixture
def native_case(tmp_path, monkeypatch):
    def deny_network(*_args, **_kwargs):
        raise AssertionError("#18 quota wait 回归禁止访问真实网络/Provider")

    monkeypatch.setattr(requests.sessions.Session, "request", deny_network)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", deny_network)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", deny_network)
    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, deny_network)
    monkeypatch.setattr(socket, "create_connection", deny_network)
    monkeypatch.setattr(socket, "getaddrinfo", deny_network)
    with initialized_web_runtime():
        case = NativeCase(tmp_path)
        # 保留真实生命周期，仅用可推进时钟让 poll/claim 自然到期。
        monkeypatch.setattr(effects.time, "time", lambda: case.clock[0])
        yield case


def source_for(case):
    return {
        "version": 1, "kind": "fixture_files", "owner": case.owner,
        "session_id": case.sid, "credential_generation": 1,
        "read": {"tool": READ, "arguments": dict(A)},
        "baseline": {"selected": [{"identity": PRIVATE}]},
    }


class ReadQuota(MediaFluxToolRateLimiter):
    """与 read_wait_lifecycle 的 Quota 一样，只拒绝尚未开始的本地 READ。"""

    def __init__(self, clock):
        self.clock = clock
        self.denials = []

    async def acquire(self, *, tool_name, **kwargs):
        if tool_name == READ:
            self.denials.append(tool_name)
            raise ToolRateLimitError(RateLimitDecision(
                False, 6, 1, 0, retry_after_seconds=10,
                retry_at_epoch=self.clock[0] + 10, reason="quota",
            ))
        await super().acquire(tool_name=tool_name, **kwargs)


def receipt(state, plan_id):
    rows = [deepcopy(row) for row in state.conversation
            if row.get("completion_receipt_id") == plan_id]
    assert len(rows) == 1, "原失败 receipt 必须唯一"
    assert json.loads(rows[0]["content"][rows[0]["content"].index("{"):])["ok"] is False
    return rows[0]


def assert_private(case, scripts, state):
    events = asyncio.run(case.cold_store().list_events(owner=case.owner, session_id=case.sid))
    exposed = json.dumps({
        "model": [script.model_requests for script in scripts],
        "wire": [script.requests for script in scripts],
        "events": events, "conversation": state.conversation,
    }, ensure_ascii=False)
    assert PRIVATE not in exposed
    assert "reobservation_source" not in exposed
    for script in scripts:
        assert len(script.requests) == len(script.model_requests)
        script.assert_user_inputs([GOAL])


@pytest.mark.parametrize("tracked_task", [False, True])
def test_synchronous_failed_confirm_uses_effect_wait_with_or_without_task(native_case, tracked_task):
    """普通用户也能恢复确认后的诊断，不要求手工先建任务清单。"""
    case = native_case
    source = source_for(case)
    frozen_source = deepcopy(source)
    prepare = case.media.prepare
    inspect = case.media.inspect
    observed = []

    def read(arguments, context):
        assert context.diagnostic_only
        assert context.reobservation_source == frozen_source
        observed.append(deepcopy(context.reobservation_source))
        return inspect(arguments, context)

    case.media.inspect = read

    def freeze(arguments, context):
        result = prepare(arguments, context)
        return replace(result, metadata={"reobservation_source": deepcopy(source)})

    case.media.prepare = freeze
    case.build(["read_a", "finish"])
    if tracked_task:
        # 模型经原生session SDK建立任务与预算，不手写task/read_wait。
        from app.agent.kernel.sandbox import run_code_tool
        case.session.catalog.register(run_code_tool(case.session.pipeline))
        case.script.steps[:0] = ["core_task"]
    case.session.continuation_binding = _capture_creation_binding()
    quota = ReadQuota(case.clock)
    case.session.pipeline.rate_limiter = quota

    async def run():
        preview = [event async for event in case.session.run(AgentInput(
            message=GOAL, owner=case.owner, session_id=case.sid, request_id="source-turn",
        ))]
        case.events.extend(preview)
        assert preview[-1].payload["status"] == "approval_required"
        plan_id = preview[-1].payload["plan_id"]
        initial = await case.cold_store().load(owner=case.owner, session_id=case.sid)
        users = case.users(initial)
        assert len(users) == 1 and users[0]["history_id"]
        assert users == [{"role": "user", "content": GOAL, "history_id": users[0]["history_id"]}]
        if tracked_task:
            assert initial.metadata["task"]["root_goal"] == GOAL
            assert initial.metadata["read_wait"]["state"] == "idle"
        with db.get_conn() as conn:
            assert conn.execute("SELECT COUNT(*) FROM agent_confirmations").fetchone()[0] == 1
        plan = await case.session.pipeline.get_pending_plan(owner=case.owner, session_id=case.sid)
        assert plan.metadata["reobservation_source"] == source
        events, state = await case.confirm(plan_id)
        assert events[-1].payload["status"] == "waiting"
        assert quota.denials == [READ]
        assert any(event.type is AgentEventType.TOOL_FAILED
                   and event.payload.get("code") == "rate_limited" for event in events)
        assert (state.metadata.get("read_wait") or {}).get("state") not in {"pending", "claimed", "running"}
        row = state.metadata["effect_waits"][plan_id]
        record = deepcopy(row["continuation"])
        assert row["state"] == "terminal" and row["final_result"]["ok"] is False
        assert record["state"] == "pending" and record["kind"] == "effect"
        assert (state.metadata["read_wait"]["budget"] if tracked_task else record["budget"])["wait_segments"] == 1
        budget = state.metadata["read_wait"]["budget"] if tracked_task else record["budget"]
        assert budget["model_spent"] == (3 if tracked_task else 2)
        assert budget["token_spent"] > 0 and budget["active_since"] is None
        if tracked_task:
            assert record["budget_task_id"] == state.metadata["task"]["task_id"]
            assert "budget" not in record  # 同一task只有一份可消费预算。
        assert await case.cold_store().due_continuations() == []
        case.assert_io(prepares=["A"], executions=["A"], reads=0, requests=3 if tracked_task else 2)
        case.assert_source(state, users)
        failed = [event for event in events if event.type is AgentEventType.EFFECT_FAILED]
        assert len(failed) == 1 and failed[0].payload["plan_id"] == plan_id
        assert failed[0].payload["result"]["ok"] is False
        assert not any(event.type is AgentEventType.EFFECT_COMPLETED for event in events)
        original_receipt = receipt(state, plan_id)
        followup = case.script.requests[2 if tracked_task else 1]
        assert any(row.get("role") == "user" and row.get("content") == (
            "运行时执行结果通知（不是用户新指令或新增写入授权）：\n" + original_receipt["content"]
        ) for row in followup["messages"])
        duplicate, unchanged = await case.confirm(plan_id)
        assert duplicate[-1].type is AgentEventType.TURN_FAILED
        case.assert_source(unchanged, users)
        assert unchanged.metadata["effect_waits"][plan_id] == row
        source["baseline"]["selected"].clear()
        case.clock[0] = record["due_at"]
        candidate, = await case.cold_store().due_continuations()
        assert candidate["kind"] == "effect"
        session, resumed = rebuild(case, ["finish"], "sync-effect-cold")
        assert await session.resume_wait(candidate=candidate)
        restored = await case.cold_store().load(owner=case.owner, session_id=case.sid)
        case.assert_source(restored, users)
        assert receipt(restored, plan_id) == original_receipt
        assert case.media.executed == ["A"] and len(case.media.reads) == 1
        assert observed == [frozen_source]
        assert restored.metadata["effect_waits"][plan_id]["continuation"]["state"] == "terminal"
        assert not await session.resume_wait(candidate=candidate)
        return restored, [case.script, resumed]

    state, scripts = asyncio.run(run())
    assert_private(case, scripts, state)


class EffectScript(NativeScript):
    """真实领域 WRITE 与本地业务 READ 的原生 HTTP 离线脚本。"""

    def __init__(self, steps, prefix, change_arguments):
        super().__init__("tmdb")
        self.steps = list(steps)
        self.prefix = prefix
        self.change_arguments = change_arguments

    async def respond(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        assert self.steps, "续行不能超出原生离线脚本或重复调用模型"
        step = self.steps.pop(0)
        if step == "finish":
            # 故意过度宣称：成功 READ 不能洗掉失败 write。
            message = {"content": "诊断读取成功，所以之前的改名操作已成功完成。"}
        else:
            name, arguments = (WRITE, self.change_arguments) if step == "prepare" else (READ, A)
            assert step in {"prepare", "read"}
            message = {"content": "", "tool_calls": [{
                "id": self.prefix + "-" + step, "type": "function",
                "function": {"name": name.replace(".", "__"),
                             "arguments": json.dumps(arguments)},
            }]}
        return httpx.Response(200, json={"choices": [{
            "message": message,
            "finish_reason": "stop" if step == "finish" else "tool_calls",
        }]})


@pytest.fixture
def background_case(native_case):
    case = native_case
    source = source_for(case)
    observed = []
    prepared = []
    inspect = case.media.inspect

    async def freeze(arguments, context):
        # 只给真实领域预检附加本次私有来源，不伪造确认/受理/完成状态。
        assert context.reobservation_source is None
        assert real.prepare is not None
        result = await real.prepare(arguments, context)
        assert isinstance(result, PreparedEffect)
        prepared.append(deepcopy(arguments))
        return replace(result, metadata={**result.metadata, "reobservation_source": deepcopy(source)})

    def read(arguments, context):
        observed.append(deepcopy(context.reobservation_source))
        return inspect(arguments, context)

    case.media.inspect = read
    read_spec = case.media.tools()[1]
    case.media.tools = lambda: [replace(
        real, prepare=freeze, description="将媒体文件改名 rename 云盘 source 冻结确认",
    ), read_spec]

    class FailedWriteClient(_OfflineClient):
        def __init__(self):
            super().__init__()
            self.write_calls = []

        def rename(self, file_id, new_name):
            self.write_calls.append((file_id, new_name))
            # 不 mock 回执或 execute_confirmed：worker 的 Provider 夹具实际改名
            # 临时媒体文件，再丢失 ack；真实领域异常处理据此形成失败终态。
            case.media.execute(A, case.media.fingerprint("A"), None)
            raise RuntimeError("offline Provider lost acknowledgement after actual rename")

    client = FailedWriteClient()
    with _offline_runtime(case.media.root, client) as stack, _paused_manager(), patch.object(
        effects, "wait_for_effect_completion",
        partial(effects.wait_for_effect_completion, timeout_seconds=0),
    ):
        real = _spec("fs")
        case.change_arguments = _domain_arguments(
            "fs", client, ToolContext(owner=case.owner, session_id=case.sid), stack,
        )
        yield case, source, observed, prepared, client


def rebuild(case, steps, prefix):
    """新 SQLite store、pipeline、session、confirmation store 和原生 HTTP adapter。"""
    case.store = case.cold_store()
    case.build([])
    script = EffectScript(steps, prefix, getattr(case, "change_arguments", None))
    case.script = script
    case.session.model = script.adapter
    case.session.continuation_binding = _capture_creation_binding()
    return case.session, script


async def failed_background_candidate(case, prepared, client):
    from app.agent.kernel.continuation import effect_payload

    session, script = rebuild(case, ["prepare"], "source")
    events = [event async for event in session.run(AgentInput(
        message=GOAL, owner=case.owner, session_id=case.sid, request_id="source-turn",
    ))]
    case.events.extend(events)
    assert events[-1].payload["status"] == "approval_required"
    initial = await case.cold_store().load(owner=case.owner, session_id=case.sid)
    users = case.users(initial)
    assert len(users) == 1 and users[0]["history_id"]
    assert users == [{"role": "user", "content": GOAL, "history_id": users[0]["history_id"]}]
    plan_id = events[-1].payload["plan_id"]
    assert prepared == [case.change_arguments]
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM agent_confirmations").fetchone()[0] == 1
        assert conn.execute("PRAGMA database_list").fetchone()[2] == str(db.resolve_db_path())
    frozen = await session.pipeline.get_pending_plan(owner=case.owner, session_id=case.sid)
    source = deepcopy(frozen.metadata["reobservation_source"])
    confirmation, state = await case.confirm(plan_id)
    assert confirmation[-1].payload["status"] == "waiting"
    case.assert_source(state, users)
    assert not script.steps and len(script.requests) == 1
    assert not case.media.executed and not client.write_calls
    assert not state.metadata.get("read_wait")
    wait = state.metadata["effect_waits"][plan_id]
    assert wait["continuation"]["kind"] == "effect"
    assert wait["continuation"]["state"] == "waiting"
    assert await case.cold_store().due_continuations() == []
    with db.get_conn() as conn:
        rows = conn.execute("SELECT * FROM organize_operation_jobs").fetchall()
    assert len(rows) == 1 and rows[0]["status"] == "pending"
    worker = _run_real_worker(jobs.claim_organize_operation_job(rows[0]["job_id"]))
    get_organize_operation_job_result = jobs.get_organize_operation_job(rows[0]["job_id"])
    assert get_organize_operation_job_result is not None
    assert get_organize_operation_job_result["status"] in {"failed", "partial"}
    assert client.write_calls == [("rename", "admitted.mp4")]
    assert case.media.executed == ["A"] and case.media.target("A").is_file()
    assert not case.media.source("A").exists()
    # 推进真实 next_poll_at；不修改 wait 记录或手造候选/租约。
    case.clock[0] = wait["next_poll_at"] + 1
    with patch.object(organize_tasks, "get_organize_manager", lambda: worker):
        assert await effects.poll_effect_receipts(case.cold_store()) == 1
    state = await case.cold_store().load(owner=case.owner, session_id=case.sid)
    original_receipt = receipt(state, plan_id)
    wait = state.metadata["effect_waits"][plan_id]
    assert wait["final_result"]["ok"] is False
    assert effect_payload(state, wait["continuation"])["origin"]["reobservation_source"] == source
    # 冷 store 只读出密文来源；公开候选与普通 state JSON 不能夹带 source。
    assert PRIVATE not in json.dumps(state.to_payload(), ensure_ascii=False)
    candidates = await case.cold_store().due_continuations()
    assert len(candidates) == 1 and candidates[0]["kind"] == "effect"
    assert candidates[0]["wait_id"] == plan_id
    assert PRIVATE not in json.dumps(candidates, ensure_ascii=False)
    case.assert_source(state, users)
    return plan_id, source, original_receipt, candidates[0], script, users


async def park_read_quota(case, candidate, original_receipt, prefix):
    direct = bool(candidate.get("anchor"))
    session, script = rebuild(case, [] if direct else ["read"], prefix)
    quota = ReadQuota(case.clock)
    session.pipeline.rate_limiter = quota
    assert await session.resume_wait(candidate=candidate)
    state = await case.cold_store().load(owner=case.owner, session_id=case.sid)
    assert quota.denials == [READ] and not script.steps
    assert len(script.requests) == (0 if direct else 1)
    assert not state.metadata.get("read_wait"), "实际等待仍为 effect，不是 read_quota"
    record = state.metadata["effect_waits"][candidate["wait_id"]]["continuation"]
    assert record["kind"] == "effect" and record["state"] == "pending"
    assert record["due_at"] == case.clock[0] + 10
    assert record["budget"]["wait_segments"] >= 1
    assert receipt(state, candidate["wait_id"]) == original_receipt
    assert not case.media.reads
    assert await case.cold_store().due_continuations() == []
    return deepcopy(record), state, script


def assert_one_write(case, prepared, client, plan_id, state, users):
    assert prepared == [case.change_arguments] and client.write_calls == [("rename", "admitted.mp4")]
    assert case.media.executed == ["A"]
    assert len(case.media.journal.read_text().splitlines()) == 1
    assert case.media.target("A").read_bytes() == b"offline-media-A"
    assert case.media.source("B").is_file() and not case.media.target("B").exists()
    case.assert_source(state, users)
    assert not state.pending_effect_plan_id
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM organize_operation_jobs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM agent_confirmations").fetchone()[0] == 0
        actions = conn.execute("SELECT confirmation_id FROM agent_action_history").fetchall()
        assert len(actions) == 1 and actions[0]["confirmation_id"].startswith(plan_id + "-")


@pytest.mark.parametrize("quota_segments", [1, 2])
def test_failed_effect_quota_cold_resume_preserves_private_source_and_original_receipt(
    background_case, quota_segments,
):
    case, mutable_source, observed, prepared, client = background_case
    scripts = []

    async def run():
        plan_id, source, original_receipt, candidate, script, users = await failed_background_candidate(
            case, prepared, client,
        )
        scripts.append(script)
        for index in range(quota_segments):
            record, state, script = await park_read_quota(
                case, candidate, original_receipt, f"quota-{index}",
            )
            case.assert_source(state, users)
            scripts.append(script)
            assert record["budget"]["wait_segments"] == index + 1
            assert observed == [], "额度拒绝发生在业务 READ/source 恢复之前"
            # 旧确认不能消费等待、启动模型或重放 write。
            duplicate, unchanged = await case.confirm(plan_id)
            assert duplicate[-1].type is AgentEventType.TURN_FAILED
            assert unchanged.conversation == state.conversation
            assert unchanged.metadata["effect_waits"] == state.metadata["effect_waits"]
            assert len(script.requests) == (1 if index == 0 else 0)
            case.clock[0] = record["due_at"]
            candidates = await case.cold_store().due_continuations()
            assert len(candidates) == 1 and candidates[0]["kind"] == "effect"
            candidate = candidates[0]
        # 不能借测试闭包携带来源：改掉预检侧副本，恢复仍须来自已保存的密文。
        mutable_source["baseline"]["selected"].clear()
        session, resumed = rebuild(case, ["finish"], "cold")
        scripts.append(resumed)
        assert await session.resume_wait(candidate=candidate)
        state = await case.cold_store().load(owner=case.owner, session_id=case.sid)
        assert observed == [source]
        assert len(case.media.reads) == 1 and not resumed.steps
        assert len(resumed.requests) == 1
        assert receipt(state, plan_id) == original_receipt
        record = state.metadata["effect_waits"][plan_id]["continuation"]
        assert record["state"] == "terminal" and record["terminal_reason"] == "partial"
        assert record["budget"]["wait_segments"] == quota_segments
        assert record["budget"]["wait_seconds_spent"] == quota_segments * 10
        assert record["budget"]["model_spent"] == 3
        assert record["budget"]["tool_spent"] == quota_segments + 2
        assert await case.cold_store().due_continuations() == []
        events = await case.cold_store().list_events(owner=case.owner, session_id=case.sid)
        terminal = [event for event in events if event["type"] == "turn.completed"]
        assert terminal[-1]["payload"]["status"] == "partial", "成功 READ 不能洗成 write 成功"
        tool_rows = [row for row in state.conversation if row.get("role") == "tool"]
        assert len({row["tool_call_id"] for row in tool_rows}) == len(tool_rows)
        assert sum("rate_limited" in row["content"] for row in tool_rows) == quota_segments
        assert_one_write(case, prepared, client, plan_id, state, users)
        # terminal candidate 的重复领取同样不能重启模型或重放任何业务 IO。
        assert not await session.resume_wait(candidate=candidate)
        assert len(resumed.requests) == 1 and observed == [source]
        return state

    state = asyncio.run(run())
    assert_private(case, scripts, state)


@pytest.mark.parametrize("cancel_entry", ["request", "session"])
def test_cancelled_failed_effect_quota_wait_never_restores_source_or_replays_write(background_case, cancel_entry):
    case, _source, observed, prepared, client = background_case
    scripts = []

    async def run():
        plan_id, _saved_source, original_receipt, candidate, script, users = await failed_background_candidate(
            case, prepared, client,
        )
        scripts.append(script)
        record, before, script = await park_read_quota(case, candidate, original_receipt, "cancel-quota")
        case.assert_source(before, users)
        scripts.append(script)
        case.clock[0] = record["due_at"]
        candidates = await case.cold_store().due_continuations()
        assert len(candidates) == 1 and candidates[0]["kind"] == "effect"
        stale_candidate = candidates[0]
        session, unused = rebuild(case, ["read", "finish"], "cancelled")
        scripts.append(unused)
        # 两个公开入口都必须持久撤销续行，不能只取消本地消费者。
        if cancel_entry == "request":
            assert await session.cancel(owner=case.owner, session_id=case.sid,
                                        request_id=record["source_request_id"])
        else:
            stopped = await stop_agent_session(
                SimpleNamespace(store=case.store, session=session), case.owner, case.sid,
            )
            assert stopped["cancelled_continuations"] == [plan_id]
            assert stopped["status"] == "stopped" and stopped["stopped"] is True
            assert stopped["background_tasks"] == [], "失败 worker 已终态，不是被 stop 再执行/取消"
        cancelled = await case.cold_store().load(owner=case.owner, session_id=case.sid)
        saved = cancelled.metadata["effect_waits"][plan_id]["continuation"]
        assert saved["state"] == "cancelled" and saved["terminal_reason"] == "cancelled"
        assert saved["budget"] == record["budget"]
        assert cancelled.conversation == before.conversation
        assert receipt(cancelled, plan_id) == original_receipt
        assert await case.cold_store().due_continuations() == []
        from app.agent.kernel import continuation
        with patch.object(continuation, "confirmed_result", side_effect=AssertionError(
            "已取消续行不能重建 confirmed result 或恢复私有 source",
        )):
            assert not await session.resume_wait(candidate=stale_candidate)
        assert not unused.requests and not unused.model_requests
        assert observed == [] and not case.media.reads
        state = await case.cold_store().load(owner=case.owner, session_id=case.sid)
        assert state.conversation == before.conversation
        assert receipt(state, plan_id) == original_receipt
        assert_one_write(case, prepared, client, plan_id, state, users)
        return state

    state = asyncio.run(run())
    assert_private(case, scripts, state)
