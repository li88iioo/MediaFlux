"""切片 #18 核心控制流：真实 SQLite / 原生模型 wire / 临时文件 IO，非生产实测。

只隔离 Provider 与领域文件 IO。Pipeline 记录器完整委托真实实现，不 mock
execute_confirmed、确认票据、失败回执或执行计数。观察来源契约由另一切片负责。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from copy import deepcopy
from pathlib import Path

import httpx
import pytest
import requests

from app import config
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
from app.agent.kernel.projection import ReferenceValue, ToolOutcome
from app.agent.kernel.references import ReferenceError
from app.agent.kernel.session import AgentSession
from app.agent.kernel.state_types import AgentInput, CancellationToken
from app.agent.kernel.tool_calls import ToolCallContext
from app.agent.models import ToolResult
from app.agent.owner_routes import web_kernel_owner
from tests.support import initialized_web_runtime
from tests.test_agent_task_first_slice import OWNER, NativeScript

READ = "library.observe_fixture"
WRITE = "library.rename_observed_fixture"
GOAL = "读取当前媒体文件并将 movie.mkv 改名为 renamed.mkv；若引用或状态变化，重新读取后给我新确认卡，不得自动执行。"
OLD_BYTES = b"offline-movie-version-one"
NEW_BYTES = b"offline-movie-version-two"
OFFLINE = "离线原生回归（不是生产实测）"
EVIDENCE = Path(__file__).resolve().parents[1] / ".superpowers/tmp/agent-reference-reobservation-20261012"
WRITE_SCHEMA = {
    "type": "object", "properties": {"observation_ref": {"type": "string"}},
    "required": ["observation_ref"], "additionalProperties": False,
}


def digest(content):
    return hashlib.sha256(content).hexdigest()


class ReobservationScript(NativeScript):
    """新 WRITE 参数只能从真实 READ 的 HTTP tool 消息中取，不读取夹具后台状态。"""

    def __init__(self, followup):
        super().__init__("fixture")
        self.steps = ["read_initial", "preview_initial", *followup]
        self.calls = []
        self.initial_ref = None

    async def respond(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        assert self.steps, f"{OFFLINE}：额外模型调用或重复续行"
        step = self.steps.pop(0)
        if step == "finish":
            message = {"content": "已核对本轮工具回执；未获新确认的操作不执行。"}
            self.calls.append({"step": step})
        else:
            if step.startswith("read_"):
                name, arguments = READ, {"object": "movie"}
            else:
                observations = []
                for row in body["messages"]:
                    if row.get("role") == "tool":
                        value = json.loads(row["content"])
                        ref = value.get("reference_arguments", {}).get("observation_ref")
                        if ref:
                            observations.append(ref)
                if step == "preview_initial":
                    assert observations, f"{OFFLINE}：首次预检必须先得到真实 READ 的 ref"
                    self.initial_ref = observations[-1]
                ref = self.initial_ref if step in {"preview_initial", "preview_old"} else observations[-1]
                assert ref, f"{OFFLINE}：模型不能合成引用"
                if step == "preview_new":
                    assert ref != self.initial_ref, f"{OFFLINE}：新预检必须使用 READ 返回的新 ref"
                name, arguments = WRITE, {"observation_ref": ref}
            self.calls.append({"step": step, "tool": name, "arguments": deepcopy(arguments)})
            message = {"content": "", "tool_calls": [{
                "id": "reobserve-" + step, "type": "function",
                "function": {"name": name.replace(".", "__"), "arguments": json.dumps(arguments)},
            }]}
        return httpx.Response(200, json={"choices": [{
            "message": message, "finish_reason": "stop" if step == "finish" else "tool_calls",
        }]})


class RecordedPipeline(ToolPipeline):
    """仅旁路记录公开 execute 入参；返回、异常、执行和存储全部来自真实父实现。"""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.calls = []

    async def execute(self, name, arguments, *, context, **kwargs):
        record = {"tool": name, "arguments": deepcopy(arguments),
                  "call_id": context.call_id, "turn_id": context.turn_id}
        self.calls.append(record)
        try:
            result = await super().execute(name, arguments, context=context, **kwargs)
        except ToolPipelineError as exc:
            record["error"] = exc.code
            raise
        record["plan_id"] = result.effect_plan.plan_id if result.effect_plan else None
        return result


class FileDomain:
    def __init__(self, root):
        self.source = root / "movie.mkv"
        self.target = root / "renamed.mkv"
        self.journal = root / "mutations.jsonl"
        self.source.write_bytes(OLD_BYTES)
        self.read_failure = False
        self.reads = []
        self.prepares = []
        self.attempts = []
        self.mutations = []

    def observe(self, _arguments, _context):
        content = self.source.read_bytes()
        observation = {"object": "movie", "name": self.source.name,
                       "digest": digest(content), "size": len(content)}
        self.reads.append({**observation, "ok": not self.read_failure})
        if self.read_failure:
            return ToolResult(False, "fixture_read_failed", "临时文件读到但核对失败，不签发新引用")
        public = {"ok": True, "status": "success", "summary": "已读取当前临时媒体文件", "data": observation}
        return ToolOutcome(model_content=json.dumps(public), public_content=public,
                           refs=(ReferenceValue(kind="observation", value=observation, ttl_seconds=5),))

    @staticmethod
    def canonical_arguments(arguments):
        # 内部规范参数有 access_code；公开 schema 故意且真实地不允许它。
        return {**arguments, "access_code": ""}

    def prepare(self, arguments, _context):
        observation = arguments["observation"]
        current = digest(self.source.read_bytes())
        assert arguments["access_code"] == ""
        self.prepares.append(deepcopy(observation))
        if observation["digest"] != current:
            raise ToolPipelineError("观察值已变化，请重新读取", code="confirmation_stale")
        return PreparedEffect(
            preview={"ok": True, "summary": "将观察到的 movie.mkv 改名", "observed": observation},
            snapshot_fingerprint=current,
        )

    def execute(self, arguments, fingerprint, _context):
        assert arguments["access_code"] == ""
        actual = digest(self.source.read_bytes())
        self.attempts.append({"observed": arguments["observation"]["digest"],
                              "frozen": fingerprint, "actual": actual})
        if actual != fingerprint:
            raise ToolPipelineError("临时文件状态已改变，旧确认不可执行", code="confirmation_stale")
        self.source.rename(self.target)
        fact = {"digest": digest(self.target.read_bytes()), "name": self.target.name}
        with self.journal.open("a") as stream:
            stream.write(json.dumps(fact) + "\n")
        self.mutations.append(fact)
        return ToolResult(True, "success", "已按新确认改名临时文件", data=fact)

    def tools(self):
        return [
            KernelToolSpec(name=READ, domain="library", effect=ToolEffect.READ,
                           description="读取观察媒体文件 movie 状态快照引用",
                           input_schema={"type": "object", "properties": {"object": {"const": "movie", "type": "string"}},
                                         "required": ["object"], "additionalProperties": False}, read=self.observe),
            KernelToolSpec(name=WRITE, domain="library", effect=ToolEffect.WRITE,
                           description="根据新观察引用预检媒体文件改名，生成确认卡",
                           input_schema=deepcopy(WRITE_SCHEMA), validator=self.canonical_arguments,
                           prepare=self.prepare, execute_confirmed=self.execute),
        ]


class Case:
    def __init__(self, root):
        self.clock = [time.time()]
        self.owner = OWNER
        self.sid = "reference-reobservation-session"
        self.domain = FileDomain(root)
        self.store = self.cold_store()
        self.events = []
        self.source_users = []
        self.script = None
        self.pipeline = None

    def cold_store(self):
        return SQLiteKernelStore(secret_provider=lambda: "offline-reobservation-session", clock=lambda: self.clock[0])

    def build(self, followup):
        self.script = ReobservationScript(followup)
        catalog = ToolCatalog(self.domain.tools())
        self.pipeline = RecordedPipeline(
            catalog=catalog, state_store=self.store, reference_store=self.store,
            effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore(ttl_seconds=300, clock=lambda: self.clock[0]),
                                                     record_actions=True),
            authorization=MediaFluxAuthorizationPolicy(), rate_limiter=MediaFluxToolRateLimiter(),
            effect_lifecycle=MediaFluxEffectLifecycle(),
        )
        self.session = AgentSession(model=self.script.adapter, catalog=catalog,
                                     pipeline=self.pipeline,
                                    state_store=self.store, journal=self.store, turn_admission=MediaFluxTurnAdmission())

    async def state(self):
        return await self.cold_store().load(owner=self.owner, session_id=self.sid)

    async def preview(self):
        events = [event async for event in self.session.run(AgentInput(
            message=GOAL, owner=self.owner, session_id=self.sid, request_id="original-user"))]
        self.events.extend(events)
        assert events[-1].payload["status"] == "approval_required"
        assert self.pipeline is not None
        plan = await self.pipeline.get_pending_plan(owner=self.owner, session_id=self.sid)
        assert self.script is not None
        assert plan.arguments == {"observation_ref": self.script.initial_ref, "access_code": ""}
        assert plan.snapshot_fingerprint == digest(OLD_BYTES)
        assert "access_code" not in WRITE_SCHEMA["properties"]
        self.assert_counts(reads=1, prepares=1, attempts=0, mutations=0, models=2)
        state = await self.state()
        self.source_users = [deepcopy(row) for row in state.conversation if row.get("role") == "user"]
        assert len(self.source_users) == 1 and self.source_users[0]["history_id"]
        assert self.source_users == [{"role": "user", "content": GOAL, "history_id": self.source_users[0]["history_id"]}]
        self.assert_source(state)
        return plan

    async def invalidate(self, plan, mode):
        before = self.domain.source.stat()
        self.domain.source.write_bytes(NEW_BYTES)
        assert self.domain.source.stat().st_ino == before.st_ino, f"{OFFLINE}：同一对象内容变化"
        if mode == "reference_ttl":
            self.clock[0] += 6
            with pytest.raises(ReferenceError):
                await self.cold_store().resolve(plan.arguments["observation_ref"], owner=self.owner,
                                               session_id=self.sid, expected_kind="observation")
            assert self.clock[0] >= plan.expires_at
            assert self.pipeline is not None
            assert await self.pipeline.get_pending_plan(owner=self.owner, session_id=self.sid) is None
        else:
            original = await self.cold_store().resolve(plan.arguments["observation_ref"], owner=self.owner,
                                                     session_id=self.sid, expected_kind="observation")
            assert original["digest"] == digest(OLD_BYTES)
            assert self.clock[0] < plan.expires_at
            assert self.pipeline is not None
            assert (await self.pipeline.get_pending_plan(owner=self.owner, session_id=self.sid)).plan_id == plan.plan_id

    async def confirm(self, plan, *, owner=None):
        events = [event async for event in self.session.confirm(owner=owner or self.owner,
                  session_id=self.sid, plan_id=plan.plan_id, request_id="confirm-" + plan.plan_id)]
        self.events.extend(events)
        return events, await self.state()

    def assert_source(self, state, *, present=True):
        assert [row for row in state.conversation if row.get("role") == "user"] == (
            self.source_users if present else []
        ), f"{OFFLINE}：不得从确认按钮、模型或工具回执制造新 user goal"
        assert self.script is not None
        self.script.assert_user_inputs([GOAL])

    def assert_counts(self, *, reads, prepares, attempts, mutations, models):
        assert len(self.domain.reads) == reads
        assert len(self.domain.prepares) == prepares
        assert len(self.domain.attempts) == attempts
        assert len(self.domain.mutations) == mutations
        assert self.script is not None
        assert len(self.script.requests) == len(self.script.model_requests) == models
        facts = [json.loads(row) for row in self.domain.journal.read_text().splitlines()] if self.domain.journal.exists() else []
        assert facts == self.domain.mutations
        if mutations:
            assert not self.domain.source.exists() and self.domain.target.read_bytes() == NEW_BYTES
        else:
            assert self.domain.source.exists() and not self.domain.target.exists()

    def assert_no_canonical_repreview(self):
        assert self.pipeline is not None
        bypass = [call for call in self.pipeline.calls if call["tool"] == WRITE and "access_code" in call["arguments"]]
        assert not bypass, f"{OFFLINE}：禁止把 original_plan.arguments 重喂公开预检；真实调用记录={bypass}"

    @staticmethod
    def assert_stale_receipt(events, state, plan, *, status):
        failures = [event for event in events if event.type is AgentEventType.EFFECT_FAILED]
        assert len(failures) == 1 and failures[0].payload["plan_id"] == plan.plan_id
        result = failures[0].payload["result"]
        assert result["ok"] is False and result["status"] == "confirmation_stale"
        receipts = [row for row in state.conversation if row.get("completion_receipt_id") == plan.plan_id]
        assert len(receipts) == 1
        persisted = json.loads(receipts[0]["content"][receipts[0]["content"].index("{"):])
        assert persisted["status"] == "confirmation_stale" and persisted["ok"] is False
        assert len({event.turn_id for event in events}) == 1, f"{OFFLINE}：必须沿同一 _drive 继续"
        if status:
            builder = TurnViewBuilder()
            for event in events:
                builder.apply(event)
            view = builder.build().to_dict()
            assert view["status"] == status and view["effect_result"] == result
        return failures[0]

    def dump(self, name):
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        state = asyncio.run(self.state())
        (EVIDENCE / (name + ".json")).write_text(json.dumps({
            "scope": OFFLINE, "source_contract": "not covered by this core-control-flow slice",
            "reads": self.domain.reads, "prepares": self.domain.prepares,
            "handler_attempts": self.domain.attempts, "actual_mutations": self.domain.mutations,
            "pipeline_calls": self.pipeline.calls if self.pipeline else [],
            "model_calls": self.script.calls if self.script else [],
            "native_wire": self.script.requests if self.script else [],
            "model_requests": self.script.model_requests if self.script else [],
            "events": [event.to_dict() for event in self.events], "cold_state": state.to_payload(),
        }, ensure_ascii=False, indent=2))


@pytest.fixture
def case(tmp_path, monkeypatch, request):
    def no_network(*_args, **_kwargs):
        raise AssertionError(OFFLINE + "：禁止真实网络/Provider IO")

    monkeypatch.setattr(requests.sessions.Session, "request", no_network)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_network)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_network)
    with initialized_web_runtime():
        current = Case(tmp_path)
        try:
            yield current
        finally:
            current.dump(request.node.name)


@pytest.mark.parametrize("mode", ["snapshot_changed", "reference_ttl"])
@pytest.mark.parametrize("direct_write_first", [False, True], ids=["read-first", "reject-direct-write"])
def test_stale_confirmation_reobserves_then_requires_new_card_and_new_confirmation(case, mode, direct_write_first):
    async def run():
        followup = (["preview_old"] if direct_write_first else []) + ["read_new", "preview_new", "finish"]
        case.build(followup)
        old = await case.preview()
        await case.invalidate(old, mode)
        events, state = await case.confirm(old)
        old_attempts = int(mode == "snapshot_changed")
        case.assert_counts(reads=2, prepares=2, attempts=old_attempts, mutations=0, models=4 + int(direct_write_first))
        case.assert_source(state)
        failure = case.assert_stale_receipt(events, state, old, status="approval_required")
        observed = next(event for event in events if event.type is AgentEventType.TOOL_COMPLETED and event.payload.get("tool") == READ)
        approval = next(event for event in events if event.type is AgentEventType.EFFECT_APPROVAL_REQUIRED)
        assert events.index(failure) < events.index(observed) < events.index(approval)
        if direct_write_first:
            denied = [event for event in events if event.type is AgentEventType.TOOL_FAILED and event.payload.get("tool") == WRITE]
            assert len(denied) == 1 and denied[0].payload["code"] == "effect_diagnostic_only"
            assert events.index(denied[0]) < events.index(observed)
        new = await case.pipeline.get_pending_plan(owner=case.owner, session_id=case.sid)
        new_ref = observed.payload["result"]["reference_arguments"]["observation_ref"]
        assert new.plan_id != old.plan_id and new.arguments == {"observation_ref": new_ref, "access_code": ""}
        assert new_ref != old.arguments["observation_ref"]
        resolved = await case.cold_store().resolve(new_ref, owner=case.owner, session_id=case.sid, expected_kind="observation")
        assert resolved["digest"] == digest(NEW_BYTES) and resolved["object"] == "movie"
        assert new.snapshot_fingerprint == digest(NEW_BYTES) != old.snapshot_fingerprint
        assert new.preview["observed"] == resolved
        assert [row["digest"] for row in case.domain.prepares] == [digest(OLD_BYTES), digest(NEW_BYTES)]
        # 同一 confirm 的第一个模型请求必须带真实 stale 回执，不靠模型自述充当观察。
        assert any(json.loads(row["content"]).get("status") == "confirmation_stale"
                   for row in case.script.requests[2]["messages"] if row.get("role") == "tool")
        before_duplicate = state.to_payload()
        duplicate, unchanged = await case.confirm(old)
        assert duplicate[-1].type is AgentEventType.TURN_FAILED and duplicate[-1].payload["code"] == "confirmation_invalid"
        assert unchanged.to_payload() == before_duplicate
        case.assert_counts(reads=2, prepares=2, attempts=old_attempts, mutations=0, models=4 + int(direct_write_first))
        completed, final = await case.confirm(new)
        assert any(event.type is AgentEventType.EFFECT_COMPLETED for event in completed)
        case.assert_counts(reads=2, prepares=2, attempts=old_attempts + 1, mutations=1, models=5 + int(direct_write_first))
        case.assert_source(final)
        assert not final.pending_effect_plan_id and not case.script.steps
        receipts = [row for row in final.conversation if row.get("completion_receipt_id") in {old.plan_id, new.plan_id}]
        assert len(receipts) == 2
        case.assert_no_canonical_repreview()
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["snapshot_changed", "reference_ttl"])
def test_failed_reobservation_does_not_unlock_new_preview(case, mode):
    async def run():
        case.build(["read_new", "preview_old", "finish"])
        old = await case.preview()
        await case.invalidate(old, mode)
        case.domain.read_failure = True
        events, state = await case.confirm(old)
        case.assert_counts(reads=2, prepares=1, attempts=int(mode == "snapshot_changed"), mutations=0, models=5)
        case.assert_source(state)
        case.assert_stale_receipt(events, state, old, status="partial")
        blocked = [event for event in events if event.type is AgentEventType.TOOL_FAILED and event.payload.get("tool") == WRITE]
        assert len(blocked) == 1 and blocked[0].payload["code"] == "effect_diagnostic_only"
        assert case.domain.reads[-1]["ok"] is False
        assert not state.pending_effect_plan_id
        assert not any(event.type is AgentEventType.EFFECT_APPROVAL_REQUIRED for event in events)
        assert not case.script.steps
        case.assert_no_canonical_repreview()
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["snapshot_changed", "reference_ttl"])
def test_stale_confirmation_without_original_user_never_fabricates_goal(case, mode):
    async def run():
        case.build(["read_new", "preview_new"])
        case.script.steps = ["read_new", "preview_new"]
        lease, _ = await case.store.begin_turn(owner=case.owner, session_id=case.sid, request_id="trusted-no-source")

        async def progress(_payload):
            pass

        context = ToolCallContext(owner=case.owner, session_id=case.sid, request_id=lease.request_id,
                                  turn_id=lease.turn_id, lease=lease, cancellation=CancellationToken(), report_progress=progress)
        read = await case.pipeline.execute(READ, {"object": "movie"}, context=context)
        preview = await case.pipeline.execute(WRITE, read.outcome.public_content["reference_arguments"], context=context)
        old = preview.effect_plan
        before = await case.state()
        case.assert_source(before, present=False)
        assert not before.metadata.get("pending_effect_goal")
        await case.invalidate(old, mode)
        events, state = await case.confirm(old)
        case.assert_counts(reads=1, prepares=1, attempts=int(mode == "snapshot_changed"), mutations=0, models=0)
        case.assert_source(state, present=False)
        case.assert_stale_receipt(events, state, old, status=None)
        assert events[-1].type is AgentEventType.TURN_FAILED
        assert not state.pending_effect_plan_id and case.script.steps == ["read_new", "preview_new"]
        case.assert_no_canonical_repreview()
    asyncio.run(run())


@pytest.mark.parametrize("boundary", ["foreign_owner", "credential_rotated", "cancelled", "permission_revoked", "agent_disabled"])
def test_security_or_cancelled_ticket_never_starts_reobservation(case, boundary):
    async def run():
        if boundary == "permission_revoked":
            case.owner = "tg:v1:12345\x1f67890"
            config.set_and_save({"TG_AGENT_ENABLED": "1", "TG_CHAT_ID": "12345", "TG_AGENT_ALLOWED_USER_IDS": "67890"})
        case.build(["read_new", "preview_new"])
        old = await case.preview()
        await case.invalidate(old, "snapshot_changed")
        owner = case.owner
        if boundary == "foreign_owner":
            owner = web_kernel_owner("another-offline-user")
        elif boundary == "credential_rotated":
            # 真实轮换隔离安装的确认签名密钥；会话仓储独立测试密钥不变。
            config.set_and_save({"WEB_SECRET_KEY": "rotated-offline-confirmation-secret-only"})
        elif boundary == "cancelled":
            assert await case.session.cancel_effect(owner=case.owner, session_id=case.sid, plan_id=old.plan_id)
        elif boundary == "permission_revoked":
            config.set_and_save({"TG_AGENT_ALLOWED_USER_IDS": "99999"})
        else:
            config.set_and_save({"AGENT_ENABLED": "0"})
        before = await case.state()
        events, state = await case.confirm(old, owner=owner)
        case.assert_counts(reads=1, prepares=1, attempts=0, mutations=0, models=2)
        case.assert_source(state)
        assert events[-1].type is AgentEventType.TURN_FAILED
        expected = "authorization_denied" if boundary == "permission_revoked" else "agent_disabled" if boundary == "agent_disabled" else "confirmation_invalid"
        assert events[-1].payload["code"] == expected
        assert not any(event.type is AgentEventType.EFFECT_FAILED for event in events)
        assert case.script.steps == ["read_new", "preview_new"]
        if boundary == "foreign_owner":
            assert state.to_payload() == before.to_payload()
        case.assert_no_canonical_repreview()
    asyncio.run(run())
