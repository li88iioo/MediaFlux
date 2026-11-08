"""切片 #19：失败确认写的原生续行回归，不是生产 Provider 实测。

真实 AgentSession/ToolPipeline、SQLite 会话/持久化确认票据及原生 HTTP 模型 wire。
仅模型 Provider 使用 NativeScript/MockTransport；领域 IO 为临时媒体文件的真实
改名、目录读取和索引核验，不 mock execute_confirmed、回执、会话或执行次数。

运行：.venv/bin/python -m pytest tests/test_agent_failed_effect_followup.py -q
证据：.superpowers/tmp/agent-failed-effect-20261012/（不提交）。
"""
from __future__ import annotations
from tests.agent_task_native_helpers import task_call

import asyncio
import hashlib
import json
import threading
import time
from pathlib import Path

import httpx
import pytest
import requests

from app import config
from app import database as db
from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.errors import ToolPipelineError
from app.agent.kernel.adapters import TurnViewBuilder
from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.effects import ConfirmationEffectPlanStore
from app.agent.kernel.tool_calls import PreparedEffect
from app.agent.kernel.events import AgentEventType
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.ports.mediaflux_effects import MediaFluxEffectLifecycle
from app.agent.kernel.ports.mediaflux_policy import (
    MediaFluxAuthorizationPolicy,
    MediaFluxToolRateLimiter,
    MediaFluxTurnAdmission,
)
from app.agent.kernel.projection import ToolOutcome
from app.agent.kernel.session import AgentSession
from app.agent.kernel.state_types import AgentInput, CancellationToken, SessionBusyError
from app.agent.kernel.tool_calls import ToolCallContext
from app.agent.models import ToolResult
from app.agent.public_view import public_result_state
from tests.support import initialized_web_runtime
from tests.test_agent_task_first_slice import OWNER, NativeScript

WRITE = "library.fixture_rename"
READ = "library.fixture_inspect"
A = {"entry": "A"}
B = {"entry": "B"}
GOAL = (
    "将媒体文件 A.mkv 改名为 renamed-a.mkv；若写入失败或状态未知，"
    "继续读取真实文件状态核对。核对后可为独立文件 B.mkv 改名为 renamed-b.mkv "
    "准备新确认卡，但不能自动执行 B。"
)
FAILURE_SUMMARY = "文件已经改名，但业务索引未完成；操作 A 未核验成功。"
MODEL_OVERCLAIM = "诊断读取成功，所以操作 A 已成功完成。"
FAULTS = ("returned_tool_result", "returned_tool_outcome", "throw_unknown", "verify_failure")
EVIDENCE = Path(__file__).resolve().parents[1] / ".superpowers/tmp/agent-failed-effect-20261012"
OFFLINE = "离线原生回归（不是生产实测）"


class FailedEffectScript(NativeScript):
    """只替换原生 HTTP 响应；保留真实模型 request -> wire -> decode 链路。"""

    def __init__(self, followup):
        super().__init__("tmdb")
        self.steps = ["preview_a", *followup]
        self.call_ids = []
        self.answer = MODEL_OVERCLAIM

    async def respond(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        assert self.steps, f"{OFFLINE}：模型请求超出脚本，可能存在重复续行"
        step = self.steps.pop(0)
        self.call_ids.append(step)
        if step == "finish":
            message = {"content": self.answer}
        else:
            name, arguments = {
                "preview_a": (WRITE, A),
                "retry_a": (WRITE, A),
                "preview_b": (WRITE, B),
                "read_a": (READ, A),
                "core_task": task_call({
                    "operation": "create", "source": GOAL,
                    "root_goal": GOAL, "constraints": ["不能自动执行 B"],
                    "obligations": [{
                        "id": "media-rename", "description": "核对媒体文件实际改名状态",
                        "required": True,
                        "acceptance": {"kind": "media_rename", "criterion": "实际文件名称与索引一致"},
                    }],
                }),
            }[step]
            message = {
                "content": "",
                "tool_calls": [{
                    "id": "failed-effect-" + step,
                    "type": "function",
                    "function": {
                        "name": name.replace(".", "__"),
                        "arguments": json.dumps(arguments),
                    },
                }],
            }
        return httpx.Response(200, json={"choices": [{
            "message": message,
            "finish_reason": "stop" if step == "finish" else "tool_calls",
        }]})


class MediaFixture:
    """业务 IO 夹具：实际媒体文件与旧索引；计数来自实际 handler 调用。"""

    def __init__(self, root):
        self.root = root
        self.fault = "returned_tool_result"
        self.read_ok = True
        self.read_status = "success"
        self.omit_read_ok = False
        self.prepared = []
        self.executed = []
        self.verified = []
        self.reads = []
        self.journal = root / "mutations.jsonl"
        self.index = root / "media-index.json"
        for entry in ("A", "B"):
            self.source(entry).write_bytes(("offline-media-" + entry).encode())
        self.index.write_text(json.dumps({"A": "A.mkv", "B": "B.mkv"}))

    def source(self, entry):
        return self.root / (entry + ".mkv")

    def target(self, entry):
        return self.root / ("renamed-" + entry.lower() + ".mkv")

    def fingerprint(self, entry):
        source = self.source(entry)
        if not source.is_file():
            raise ToolPipelineError("原文件已不存在，请先核对实际状态", code="precondition_failed")
        return hashlib.sha256(source.read_bytes()).hexdigest()

    def prepare(self, arguments, _context):
        entry = arguments["entry"]
        self.prepared.append(entry)
        fingerprint = self.fingerprint(entry)
        return PreparedEffect(
            preview={"summary": "将 " + entry + ".mkv 改名", "entry": entry},
            snapshot_fingerprint=fingerprint,
        )

    def execute(self, arguments, fingerprint, _context):
        entry = arguments["entry"]
        self.executed.append(entry)
        if self.fingerprint(entry) != fingerprint:
            raise ToolPipelineError("文件内容已变化", code="precondition_failed")
        # 在抛异常/返回失败前真正修改文件，并落盘事实，不能用模拟回执冒充执行。
        self.source(entry).rename(self.target(entry))
        with self.journal.open("a") as stream:
            stream.write(json.dumps({"entry": entry, "name": self.target(entry).name}) + "\n")
        if self.fault == "throw_unknown":
            raise RuntimeError("offline handler lost acknowledgement after actual rename")
        if self.fault == "verify_failure":
            # 文件已改名，实际旧索引未更新；verify 会真实读取索引并发现不一致。
            return ToolResult(True, "success", "文件改名返回成功", data={"entry": entry})
        failure = ToolResult(False, "fixture_effect_failed", FAILURE_SUMMARY, data={
            "entry": entry, "name": self.target(entry).name, "index_updated": False,
        })
        if self.fault == "returned_tool_outcome":
            content = failure.to_dict()
            return ToolOutcome(model_content=json.dumps(content, ensure_ascii=False), public_content=content)
        return failure

    def verify(self, arguments, _result, _context):
        entry = arguments["entry"]
        self.verified.append(entry)
        actual_index = json.loads(self.index.read_text())
        return actual_index[entry] == self.target(entry).name and self.target(entry).is_file()

    def inspect(self, arguments, _context):
        entry = arguments["entry"]
        names = sorted(path.name for path in self.root.glob("*.mkv"))
        self.reads.append({"entry": entry, "names": names})
        assert self.target("A").is_file(), f"{OFFLINE}：READ 必须在实际写入之后"
        result = ToolResult(
            self.read_ok, self.read_status if self.read_ok else "fixture_read_failed",
            "已读取真实媒体文件目录" if self.read_ok else "诊断读取未核验完成",
            data={"entry": entry, "names": names},
        )
        if self.omit_read_ok:
            content = result.to_dict()
            content.pop("ok")
            return ToolOutcome(model_content=json.dumps(content, ensure_ascii=False), public_content=content)
        return result

    def tools(self):
        schema = {
            "type": "object", "properties": {"entry": {"type": "string", "enum": ["A", "B"]}},
            "required": ["entry"], "additionalProperties": False,
        }
        return [
            KernelToolSpec(
                name=WRITE, domain="library", description="媒体文件 A B 改名 rename 新确认卡",
                input_schema=schema, effect=ToolEffect.WRITE, prepare=self.prepare,
                execute_confirmed=self.execute,
                verify=self.verify if self.fault == "verify_failure" else None,
            ),
            KernelToolSpec(
                name=READ, domain="library", description="读取媒体文件实际名称状态 诊断 核对 inspect",
                input_schema=schema, effect=ToolEffect.READ, read=self.inspect,
            ),
        ]


class PausedMutationMedia(MediaFixture):
    """真实改名后暂停 handler 回执，用事件确定停止发生在确认保护期间。"""

    def __init__(self, root):
        super().__init__(root)
        self.mutated = threading.Event()
        self.release = threading.Event()

    def execute(self, arguments, fingerprint, _context):
        result = super().execute(arguments, fingerprint, _context)
        self.mutated.set()
        assert self.release.wait(10), f"{OFFLINE}：实际写入暂停未及时释放"
        return result


class PausedProviderScript(FailedEffectScript):
    """只暂停离线 Provider 的 HTTP 返回，不 mock 调度/取消/发布权限。"""

    def __init__(self, steps, *, pause_step):
        super().__init__([])
        self.steps = list(steps)
        self.pause_step = pause_step
        self.waiting = asyncio.Event()
        self.release = asyncio.Event()

    async def respond(self, request):
        reply = await super().respond(request)
        if self.call_ids[-1] == self.pause_step:
            self.waiting.set()
            await self.release.wait()
        return reply


class NativeCase:
    def __init__(self, root):
        self.media = MediaFixture(root)
        self.clock = [time.time()]
        self.owner = OWNER
        self.sid = "failed-effect-followup-session"
        self.store = self.cold_store()
        self.script = None
        self.session = None
        self.events = []
        self.extra_scripts = []

    def cold_store(self):
        return SQLiteKernelStore(secret_provider=lambda: "offline-failed-effect-only", clock=lambda: self.clock[0])

    def build(self, followup):
        self.script = FailedEffectScript(followup)
        catalog = ToolCatalog(self.media.tools())
        pipeline = ToolPipeline(
            catalog=catalog, state_store=self.store, reference_store=self.store,
            effect_store=ConfirmationEffectPlanStore(
                SQLiteConfirmationStore(ttl_seconds=60, clock=lambda: self.clock[0]), record_actions=True,
            ),
            authorization=MediaFluxAuthorizationPolicy(), rate_limiter=MediaFluxToolRateLimiter(),
            effect_lifecycle=MediaFluxEffectLifecycle(),
        )
        self.session = AgentSession(
            model=self.script.adapter, catalog=catalog,
             pipeline=pipeline,
            state_store=self.store, journal=self.store, turn_admission=MediaFluxTurnAdmission(),
        )

    async def preview(self):
        assert self.session is not None
        events = [event async for event in self.session.run(
            AgentInput(message=GOAL, owner=self.owner, session_id=self.sid, request_id="source-turn")
        )]
        self.events.extend(events)
        assert events[-1].payload["status"] == "approval_required", f"{OFFLINE}：初始 A 卡未形成"
        plan_id = events[-1].payload["plan_id"]
        state = await self.store.load(owner=self.owner, session_id=self.sid)
        users = self.users(state)
        assert len(users) == 1 and users[0]["history_id"]
        assert users == [{"role": "user", "content": GOAL, "history_id": users[0]["history_id"], "history_order": 0}]
        self.assert_io(prepares=["A"], executions=[], reads=0, requests=1)
        with db.get_conn() as conn:
            actual_database = conn.execute("PRAGMA database_list").fetchone()[2]
        assert Path(actual_database).resolve() == db.resolve_db_path().resolve()
        return plan_id, self.users(state)

    async def confirm(self, plan_id):
        assert self.session is not None
        events = [event async for event in self.session.confirm(
            owner=self.owner, session_id=self.sid, plan_id=plan_id, request_id="confirm-turn",
        )]
        self.events.extend(events)
        return events, await self.cold_store().load(owner=self.owner, session_id=self.sid)

    @staticmethod
    def users(state):
        return [dict(row) for row in state.conversation if row.get("role") == "user"]

    def assert_io(self, *, prepares, executions, reads, requests):
        assert self.media.prepared == prepares, f"{OFFLINE}：预检次数/动作不符"
        assert self.media.executed == executions, f"{OFFLINE}：实际执行次数/动作不符"
        assert len(self.media.reads) == reads, f"{OFFLINE}：没有沿原 confirm 调用自动读取诊断"
        assert self.script is not None
        assert len(self.script.requests) == len(self.script.model_requests) == requests, (
            f"{OFFLINE}：原生模型请求数不符，不能要求额外用户消息"
        )
        mutations = [json.loads(row) for row in self.media.journal.read_text().splitlines()] if self.media.journal.exists() else []
        assert [row["entry"] for row in mutations] == executions, f"{OFFLINE}：文件变更日志与执行计数不一致"
        assert self.media.source("B").is_file() and not self.media.target("B").exists(), (
            f"{OFFLINE}：新卡 B 不能在未确认时执行"
        )
        if executions:
            assert not self.media.source("A").exists()
            assert self.media.target("A").read_bytes() == b"offline-media-A"
        assert self.media.verified == (["A"] if self.media.fault == "verify_failure" and executions else [])

    def assert_source(self, state, original_users):
        assert self.users(state) == original_users, f"{OFFLINE}：确认/诊断不得新增虚构 user 消息"
        assert self.script is not None
        self.script.assert_user_inputs([GOAL])

    def assert_failure(self, events, state, plan_id):
        failed = [e for e in events if e.type is AgentEventType.EFFECT_FAILED]
        assert len(failed) == 1 and failed[0].payload["plan_id"] == plan_id
        effect_result = dict(failed[0].payload["result"])
        assert effect_result["ok"] is False
        assert not any(e.type is AgentEventType.EFFECT_COMPLETED for e in events), (
            f"{OFFLINE}：成功 READ 不得洗掉原始写失败"
        )
        receipts = [row for row in state.conversation if row.get("completion_receipt_id") == plan_id]
        assert len(receipts) == 1, f"{OFFLINE}：失败/未知回执未真实落盘"
        content = receipts[0]["content"]
        persisted = json.loads(content[content.index("{"):])
        assert persisted["ok"] is False and persisted["status"] == effect_result["status"]
        # 第一个续行 HTTP 请求已带入真实失败，不是旧预览或模型自述。
        assert self.script is not None
        if len(self.script.requests) > 1:
            failed_rows = [row for row in self.script.requests[1]["messages"] if row.get("role") == "tool"]
            assert any(json.loads(row["content"]).get("ok") is False for row in failed_rows)
        return effect_result

    def assert_terminal_failure(self, events, original_failure, *, status):
        final = events[-1]
        assert final.type is AgentEventType.TURN_COMPLETED
        assert final.payload["status"] == final.payload["turn_status"] == status, (
            f"{OFFLINE}：无新卡应 partial，新 B 卡应 approval_required"
        )
        # effect_result 是现有公开视图字段，不要求 turn.completed 新增 wire 字段。
        builder = TurnViewBuilder()
        for event in events:
            builder.apply(event)
        view = builder.build().to_dict()
        assert view["status"] == status
        assert view["effect_result"]["ok"] is False, f"{OFFLINE}：终态不能将原写判为成功"
        assert view["effect_result"] == original_failure, f"{OFFLINE}：最终视图必须保留原失败事实"
        if status == "partial":
            assert MODEL_OVERCLAIM not in final.payload["answer"]
            assert view["approval"] is None
        else:
            assert view["approval"]["plan_id"] == final.payload["plan_id"]
        assert len({event.turn_id for event in events}) == 1, f"{OFFLINE}：诊断必须沿同一 _drive，不开新回合"

    async def assert_cancelled_publication_event(self, event):
        assert event.type is AgentEventType.TURN_CANCELLED
        persisted = await self.cold_store().list_events(owner=self.owner, session_id=self.sid)
        matches = [row for row in persisted if row["event_id"] == event.event_id]
        assert matches == [event.to_dict()], f"{OFFLINE}：取消 publication 的真实事件必须落盘"

    def dump(self, name):
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        state = asyncio.run(self.cold_store().load(owner=self.owner, session_id=self.sid))
        (EVIDENCE / (name.replace("/", "_") + ".json")).write_text(json.dumps({
            "scope": OFFLINE,
            "fault": self.media.fault,
            "prepared": self.media.prepared, "executed": self.media.executed,
            "verified": self.media.verified, "reads": self.media.reads,
            "remaining_script": self.script.steps if self.script else None,
            "model_requests": self.script.model_requests if self.script else [],
            "native_wire": self.script.requests if self.script else [],
            "additional_providers": [{"model_requests": script.model_requests, "native_wire": script.requests}
                                     for script in self.extra_scripts],
            "cold_publication": {"generation": state.generation,
                                 **{key: state.metadata.get(key) for key in (
                                     "active_publication", "confirmed_publication", "stop_requested_generation")}},
            "events": [{"type": e.type.value, "turn_id": e.turn_id, "payload": dict(e.payload)} for e in self.events],
            "cold_state": state.to_payload(),
        }, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.fixture
def case(tmp_path, monkeypatch, request):
    def no_live_io(*_args, **_kwargs):
        raise AssertionError(OFFLINE + "：禁止访问真实网络/Provider")

    # 仅隔离外部 IO；MockTransport 不使用这些网络 transport。
    monkeypatch.setattr(requests.sessions.Session, "request", no_live_io)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_live_io)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_live_io)
    with initialized_web_runtime():
        current = NativeCase(tmp_path)
        try:
            yield current
        finally:
            current.dump(request.node.name)


@pytest.mark.parametrize("fault", FAULTS)
def test_failed_confirm_automatically_diagnoses_in_same_drive_and_stays_partial(case, fault):
    async def run():
        case.media.fault = fault
        case.build(["read_a", "finish"])
        plan_id, source_users = await case.preview()
        events, saved = await case.confirm(plan_id)
        case.assert_io(prepares=["A"], executions=["A"], reads=1, requests=3)
        assert not case.script.steps
        case.assert_source(saved, source_users)
        failed = case.assert_failure(events, saved, plan_id)
        case.assert_terminal_failure(events, failed, status="partial")
        assert not saved.pending_effect_plan_id
        reads = [e for e in events if e.type is AgentEventType.TOOL_COMPLETED and e.payload.get("tool") == READ]
        assert len(reads) == 1 and reads[0].payload["result"]["ok"] is True
        assert reads[0].payload["result"]["data"]["names"] == ["B.mkv", "renamed-a.mkv"]
        assert events.index(next(e for e in events if e.type is AgentEventType.EFFECT_FAILED)) < events.index(reads[0])
    asyncio.run(run())


@pytest.mark.parametrize("fault", FAULTS)
def test_successful_business_read_unlocks_only_new_b_confirmation_not_execution(case, fault):
    async def run():
        case.media.fault = fault
        case.build(["read_a", "preview_b"])
        plan_a, source_users = await case.preview()
        events, saved = await case.confirm(plan_a)
        case.assert_io(prepares=["A", "B"], executions=["A"], reads=1, requests=3)
        # 提示与执行器必须使用同一诊断状态：真实READ成功后，不能继续说“先核对”。
        prompts = [request["system_prompt"] for request in case.script.model_requests]
        assert "先用只读业务工具核对真实状态" in prompts[1]
        assert "先用只读业务工具核对真实状态" not in prompts[2]
        assert "不能再次索要这张卡的确认或重复执行" in prompts[2]
        assert not case.script.steps
        case.assert_source(saved, source_users)
        failed = case.assert_failure(events, saved, plan_a)
        case.assert_terminal_failure(events, failed, status="approval_required")
        plan_b = events[-1].payload["plan_id"]
        assert plan_b and plan_b != plan_a and saved.pending_effect_plan_id == plan_b
        active_b = await case.session.pipeline.get_pending_plan(owner=case.owner, session_id=case.sid)
        assert active_b.plan_id == plan_b and dict(active_b.arguments) == B
        # 旧确认票据不能抢占 B、发起模型或重放 A。
        duplicate, unchanged = await case.confirm(plan_a)
        assert duplicate[-1].payload["code"] == "confirmation_invalid"
        assert unchanged.to_payload() == saved.to_payload()
        case.assert_io(prepares=["A", "B"], executions=["A"], reads=1, requests=3)
    asyncio.run(run())


@pytest.mark.parametrize("direct_write", ["retry_a", "preview_b"])
def test_direct_write_before_diagnosis_is_rejected_before_prepare_or_replay(case, direct_write):
    async def run():
        case.build([direct_write, "read_a", "finish"])
        plan_id, source_users = await case.preview()
        events, saved = await case.confirm(plan_id)
        case.assert_io(prepares=["A"], executions=["A"], reads=1, requests=4)
        case.assert_source(saved, source_users)
        blocked = [e for e in events if e.type is AgentEventType.TOOL_FAILED and e.payload.get("tool") == WRITE]
        assert len(blocked) == 1 and blocked[0].payload["code"] == "effect_diagnostic_only"
        assert "重新提出" not in blocked[0].payload["message"]
        assert not any(e.type is AgentEventType.EFFECT_APPROVAL_REQUIRED for e in events)
        case.assert_terminal_failure(events, case.assert_failure(events, saved, plan_id), status="partial")
    asyncio.run(run())


@pytest.mark.parametrize("diagnostic", ["failed_business_read"])
def test_failed_business_read_does_not_unlock_new_write(case, diagnostic):
    async def run():
        step = "read_a"
        case.media.read_ok = diagnostic != "failed_business_read"
        case.build([step, "preview_b", "finish"])
        plan_id, source_users = await case.preview()
        events, saved = await case.confirm(plan_id)
        case.assert_io(prepares=["A"], executions=["A"], reads=int(step == "read_a"), requests=4)
        assert "先用只读业务工具核对真实状态" in case.script.model_requests[2]["system_prompt"]
        case.assert_source(saved, source_users)
        diagnostic_tool = READ
        observed = [e for e in events if e.type is AgentEventType.TOOL_COMPLETED and e.payload.get("tool") == diagnostic_tool]
        assert len(observed) == 1
        assert observed[0].payload["result"].get("ok") is case.media.read_ok
        blocked = [e for e in events if e.type is AgentEventType.TOOL_FAILED and e.payload.get("tool") == WRITE]
        assert len(blocked) == 1 and blocked[0].payload["code"] == "effect_diagnostic_only"
        assert events.index(observed[0]) < events.index(blocked[0])
        assert not any(e.type is AgentEventType.EFFECT_APPROVAL_REQUIRED for e in events)
        case.assert_terminal_failure(events, case.assert_failure(events, saved, plan_id), status="partial")
    asyncio.run(run())


def test_successful_real_core_task_does_not_unlock_new_write(case):
    async def run():
        case.build(["core_task", "preview_b", "finish"])
        plan_id, source_users = await case.preview()
        from app.agent.kernel.sandbox import run_code_tool
        assert case.session is not None
        case.session.catalog.register(run_code_tool(case.session.pipeline))
        events, saved = await case.confirm(plan_id)
        case.assert_io(prepares=["A"], executions=["A"], reads=0, requests=4)
        assert not case.script.steps
        case.assert_source(saved, source_users)
        completed = [e for e in events if e.type is AgentEventType.TOOL_COMPLETED and e.payload.get("tool") == "agent.run_code"]
        assert len(completed) == 1 and completed[0].payload["result"]["ok"] is True
        # 真正创建并持久化任务：不把 schema 拒绝/工具未加载冒充成功核心 READ。
        assert saved.metadata["task"]["root_goal"] == GOAL
        assert saved.metadata["task"]["obligations"][0]["acceptance"]["kind"] == "media_rename"
        assert saved.metadata["task"]["status"] != "complete"
        blocked = [e for e in events if e.type is AgentEventType.TOOL_FAILED and e.payload.get("tool") == WRITE]
        assert len(blocked) == 1 and blocked[0].payload["code"] == "effect_diagnostic_only"
        assert events.index(completed[0]) < events.index(blocked[0])
        assert not any(e.type is AgentEventType.EFFECT_APPROVAL_REQUIRED for e in events)
        assert not saved.pending_effect_plan_id
        case.assert_terminal_failure(events, case.assert_failure(events, saved, plan_id), status="partial")
    asyncio.run(run())


@pytest.mark.parametrize("fault", FAULTS)
def test_absent_original_user_source_persists_failure_but_never_starts_model(case, fault):
    async def run():
        case.media.fault = fault
        case.build(["read_a", "finish"])
        # 可信本地调用实际走 Pipeline 准备票据，但从未提供 user 原话。
        # 不篡改 publication 绑定，也不靠伪造/损坏票据模拟无来源。
        case.script.steps = ["read_a", "finish"]
        lease, _ = await case.store.begin_turn(
            owner=case.owner, session_id=case.sid, request_id="source-less-preview",
        )

        async def progress(_payload):
            pass

        result = await case.session.pipeline.execute(WRITE, A, context=ToolCallContext(
            owner=case.owner, session_id=case.sid, request_id=lease.request_id,
            turn_id=lease.turn_id, lease=lease, cancellation=CancellationToken(),
            report_progress=progress,
        ))
        plan_id = result.effect_plan.plan_id
        before = await case.cold_store().load(owner=case.owner, session_id=case.sid)
        assert before.pending_effect_plan_id == plan_id and case.users(before) == []
        assert not before.metadata.get("pending_effect_goal")
        case.assert_io(prepares=["A"], executions=[], reads=0, requests=0)
        events, saved = await case.confirm(plan_id)
        case.assert_io(prepares=["A"], executions=["A"], reads=0, requests=0)
        assert case.script.steps == ["read_a", "finish"]
        assert case.users(saved) == [], f"{OFFLINE}：不能用确认按钮文案合成 user 来源"
        case.assert_failure(events, saved, plan_id)
    asyncio.run(run())


@pytest.mark.parametrize("denial", ["expired", "permission_revoked", "agent_disabled", "damaged_ticket"])
def test_invalid_or_disallowed_confirmation_never_executes_or_starts_followup_model(case, denial):
    async def run():
        if denial == "permission_revoked":
            case.owner = "tg:v1:12345\x1f67890"
            config.set_and_save({"TG_AGENT_ENABLED": "1", "TG_CHAT_ID": "12345", "TG_AGENT_ALLOWED_USER_IDS": "67890"})
        case.build(["read_a", "finish"])
        plan_id, source_users = await case.preview()
        if denial == "expired":
            case.clock[0] += 61
        elif denial == "permission_revoked":
            config.set_and_save({"TG_AGENT_ALLOWED_USER_IDS": "99999"})
        elif denial == "agent_disabled":
            config.set_and_save({"AGENT_ENABLED": "0"})
        else:
            # 实际破坏临时 SQLite 的签名票据，不 mock claim/get_active_plan。
            with db.get_conn() as conn:
                conn.execute("UPDATE agent_confirmations SET arguments_json=? WHERE confirmation_id=?", ("[]", plan_id))
        events, saved = await case.confirm(plan_id)
        case.assert_io(prepares=["A"], executions=[], reads=0, requests=1)
        assert case.script.steps == ["read_a", "finish"]
        case.assert_source(saved, source_users)
        expected = "authorization_denied" if denial == "permission_revoked" else "agent_disabled" if denial == "agent_disabled" else "confirmation_invalid"
        assert events[-1].type is AgentEventType.TURN_FAILED and events[-1].payload["code"] == expected
        assert not any(e.type in {AgentEventType.EFFECT_COMPLETED, AgentEventType.EFFECT_FAILED} for e in events)
    asyncio.run(run())


def test_duplicate_confirmation_after_failed_write_never_replays_or_restarts_model(case):
    async def run():
        case.build(["read_a", "finish"])
        plan_id, source_users = await case.preview()
        first, saved = await case.confirm(plan_id)
        duplicate, unchanged = await case.confirm(plan_id)
        assert duplicate[-1].type is AgentEventType.TURN_FAILED
        assert duplicate[-1].payload["code"] == "confirmation_invalid"
        case.assert_io(prepares=["A"], executions=["A"], reads=1, requests=3)
        assert unchanged.to_payload() == saved.to_payload()
        case.assert_source(unchanged, source_users)
        case.assert_terminal_failure(first, case.assert_failure(first, saved, plan_id), status="partial")
    asyncio.run(run())


def test_successful_business_read_tool_outcome_without_ok_unlocks_new_b_card(case):
    async def run():
        case.media.omit_read_ok = True
        case.build(["read_a", "preview_b"])
        plan_a, source_users = await case.preview()
        events, saved = await case.confirm(plan_a)
        observed = [e for e in events if e.type is AgentEventType.TOOL_COMPLETED and e.payload.get("tool") == READ]
        assert len(observed) == 1
        assert "ok" not in observed[0].payload["result"]
        assert observed[0].payload["result"]["status"] == "success"
        assert public_result_state(observed[0].payload["result"]) == "success"
        case.assert_io(prepares=["A", "B"], executions=["A"], reads=1, requests=3)
        case.assert_source(saved, source_users)
        case.assert_terminal_failure(events, case.assert_failure(events, saved, plan_a), status="approval_required")
        plan_b = events[-1].payload["plan_id"]
        assert plan_b != plan_a and saved.pending_effect_plan_id == plan_b
        active_b = await case.session.pipeline.get_pending_plan(owner=case.owner, session_id=case.sid)
        assert active_b.plan_id == plan_b and dict(active_b.arguments) == B
        assert not case.script.steps
    asyncio.run(run())


@pytest.mark.parametrize("status,semantic_state", [
    pytest.param("failed", "failed", id="failed"),
    pytest.param("partial", "warning", id="partial"),
    pytest.param("pending", "pending", id="pending"),
])
def test_business_read_ok_true_with_non_success_state_does_not_unlock_b(case, status, semantic_state):
    async def run():
        # 覆盖要求的字面状态；使用统一分类器，不在测试里另造成功语义。
        case.media.read_status = status
        case.build(["read_a", "preview_b", "finish"])
        plan_a, source_users = await case.preview()
        events, saved = await case.confirm(plan_a)
        observed = [e for e in events if e.type is AgentEventType.TOOL_COMPLETED and e.payload.get("tool") == READ]
        assert len(observed) == 1 and observed[0].payload["result"]["ok"] is True
        assert observed[0].payload["result"]["status"] == status
        blocked = [e for e in events if e.type is AgentEventType.TOOL_FAILED and e.payload.get("tool") == WRITE]
        assert len(blocked) == 1 and blocked[0].payload["code"] == "effect_diagnostic_only", (
            f"{OFFLINE}：业务 READ ok=True/status={status} 不应解锁 B；"
            f"public_result_state={public_result_state(observed[0].payload['result'])}，"
            f"预检={case.media.prepared}，实际执行={case.media.executed}"
        )
        assert public_result_state(observed[0].payload["result"]) == semantic_state
        case.assert_io(prepares=["A"], executions=["A"], reads=1, requests=4)
        case.assert_source(saved, source_users)
        assert not any(e.type is AgentEventType.EFFECT_APPROVAL_REQUIRED for e in events)
        assert not saved.pending_effect_plan_id
        case.assert_terminal_failure(events, case.assert_failure(events, saved, plan_a), status="partial")
        assert not case.script.steps
    asyncio.run(run())


def test_stop_during_actual_failed_write_persists_stop_and_cancelled_publication(case):
    async def run():
        case.media = PausedMutationMedia(case.media.root)
        case.build(["read_a", "finish"])
        plan_a, source_users = await case.preview()
        confirming = asyncio.create_task(case.confirm(plan_a))
        try:
            assert await asyncio.to_thread(case.media.mutated.wait, 5)
            before = await case.cold_store().load(owner=case.owner, session_id=case.sid)
            live = await case.session.coordinator.describe(owner=case.owner, session_id=case.sid)
            assert live["protected"] is True and live["generation"] == before.generation
            assert case.media.target("A").is_file() and not case.media.source("A").exists()
            # 确认保护期间不能伪造 publication 撤销；真实 stop 意图必须先进入 SQLite。
            with pytest.raises(SessionBusyError, match="session operation is executing"):
                await case.session.cancel(owner=case.owner, session_id=case.sid, request_id="confirm-turn")
            assert await case.store.request_stop_generation(
                owner=case.owner, session_id=case.sid, generation=before.generation,
            )
            marked = await case.cold_store().load(owner=case.owner, session_id=case.sid)
            assert marked.generation == before.generation
            assert marked.metadata["stop_requested_generation"] == before.generation
            assert marked.metadata["confirmed_publication"] == before.metadata["confirmed_publication"]
        finally:
            case.media.release.set()
        events, saved = await asyncio.wait_for(confirming, 10)
        case.assert_io(prepares=["A"], executions=["A"], reads=0, requests=1)
        case.assert_source(saved, source_users)
        case.assert_failure(events, saved, plan_a)
        assert events[-1].type is AgentEventType.TURN_CANCELLED
        assert events[-1].request_id == "confirm-turn"
        await case.assert_cancelled_publication_event(events[-1])
        assert saved.generation == before.generation
        assert saved.metadata["stop_requested_generation"] == before.generation
        assert saved.metadata["confirmed_publication"] == before.metadata["confirmed_publication"]
        assert not saved.pending_effect_plan_id
        assert case.script.steps == ["read_a", "finish"]
        assert await case.cold_store().due_continuations() == []
    asyncio.run(run())


@pytest.mark.parametrize("cancel_new_publication", [False, True], ids=["new-user", "new-user-then-cancel"])
def test_new_user_supersedes_failed_diagnosis_and_preserves_publication_facts(case, cancel_new_publication):
    async def run():
        case.build(["read_a", "finish"])
        case.script = PausedProviderScript(["preview_a", "read_a", "finish"], pause_step="read_a")
        case.session.model = case.script.adapter
        plan_a, source_users = await case.preview()
        confirming = asyncio.create_task(case.confirm(plan_a))
        newer_task = None
        fresh_model = PausedProviderScript(["finish"], pause_step="finish")
        fresh_model.answer = "已回应新用户消息，不再推进旧改名任务。"
        case.extra_scripts.append(fresh_model)
        try:
            await asyncio.wait_for(case.script.waiting.wait(), 5)
            before = await case.cold_store().load(owner=case.owner, session_id=case.sid)
            case.session.model = fresh_model.adapter
            new_message = "新回合：只回应这条消息，不再推进旧改名任务。"

            async def collect_new_user():
                return [event async for event in case.session.run(AgentInput(
                    message=new_message, owner=case.owner, session_id=case.sid, request_id="new-user-turn",
                ))]

            newer_task = asyncio.create_task(collect_new_user())
            await asyncio.wait_for(fresh_model.waiting.wait(), 5)
            old_events, _ = await asyncio.wait_for(confirming, 10)
            assert old_events[-1].type is AgentEventType.TURN_CANCELLED
            assert old_events[-1].request_id == "confirm-turn"
            persisted = await case.cold_store().list_events(owner=case.owner, session_id=case.sid)
            assert not any(row["event_id"] == old_events[-1].event_id for row in persisted), (
                f"{OFFLINE}：已被新用户抢占的旧取消事件不得污染当前会话日志"
            )
            failed = next(event for event in old_events if event.type is AgentEventType.EFFECT_FAILED)
            assert [row for row in persisted if row["event_id"] == failed.event_id] == [failed.to_dict()]
            current = await case.cold_store().load(owner=case.owner, session_id=case.sid)
            publication = dict(current.metadata["active_publication"])
            assert current.generation == before.generation + 1
            live = await case.session.coordinator.describe(owner=case.owner, session_id=case.sid)
            assert live is not None
            assert publication == {"generation": current.generation, "turn_id": live["turn_id"],
                                   "request_id": "new-user-turn", "status": "running"}
            assert current.metadata["confirmed_publication"] == before.metadata["confirmed_publication"]
            assert live["request_id"] == "new-user-turn" and live["generation"] == current.generation
            assert persisted[-1]["turn_id"] == live["turn_id"]
            cancelled = None
            if cancel_new_publication:
                # 真正撤销新 publication；旧确认事实不能使新的停止失效。
                assert await case.session.cancel(owner=case.owner, session_id=case.sid, request_id="new-user-turn")
                cancelled = await case.cold_store().load(owner=case.owner, session_id=case.sid)
                assert cancelled.generation == current.generation + 1
                assert cancelled.metadata["active_publication"] == {**publication, "status": "cancelled"}
            else:
                fresh_model.release.set()
            newer = await asyncio.wait_for(newer_task, 10)
            case.events.extend(newer)
            saved = await case.cold_store().load(owner=case.owner, session_id=case.sid)
            if cancel_new_publication:
                assert cancelled is not None
                assert newer[-1].type is AgentEventType.TURN_CANCELLED
                await case.assert_cancelled_publication_event(newer[-1])
                assert saved.generation == cancelled.generation
                assert saved.metadata["active_publication"] == cancelled.metadata["active_publication"], (
                    f"{OFFLINE}：回合收尾不能把真实 cancelled publication 洗成 finished"
                )
            else:
                assert newer[-1].type is AgentEventType.TURN_COMPLETED
                assert saved.generation == current.generation
                assert saved.metadata["active_publication"] == {**publication, "status": "finished"}
            final_journal = await case.cold_store().list_events(owner=case.owner, session_id=case.sid)
            assert final_journal[-1] == newer[-1].to_dict()
            assert not any(row["event_id"] == old_events[-1].event_id for row in final_journal)
            case.assert_io(prepares=["A"], executions=["A"], reads=0, requests=2)
            case.assert_failure(old_events, saved, plan_a)
            users = case.users(saved)
            assert len(users) == 2 and users[-1]["history_id"] != source_users[0]["history_id"]
            assert users == source_users + [{"role": "user", "content": new_message,
                                             "history_id": users[-1]["history_id"], "history_order": 1}]
            assert len(fresh_model.requests) == len(fresh_model.model_requests) == 1
            fresh_model.assert_user_inputs([GOAL, new_message])
            assert not any(e.payload.get("tool") == READ for e in old_events if e.type is AgentEventType.TOOL_STARTED)
            assert saved.metadata["confirmed_publication"] == before.metadata["confirmed_publication"]
            assert not saved.pending_effect_plan_id
            assert await case.cold_store().due_continuations() == []
        finally:
            case.script.release.set()
            fresh_model.release.set()
            tasks = [task for task in (confirming, newer_task) if task is not None]
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    asyncio.run(run())
