"""MediaFlux lifecycle contracts: ordering, failure identity and real audit storage."""
from typing import cast
from app.agent.domains.library.missing_workflow_runtime import MissingMediaWorkflowRuntime
import asyncio
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app import database as db
from app.database import media_owner_digest
from app.agent.action_history import (
    record_confirmation_claimed,
)
from app.agent.kernel.capabilities import ToolEffect
from app.agent.kernel.tool_calls import EffectPlan, PreparedEffect
from app.agent.kernel.ports import mediaflux_effects as target
from app.agent.models import RiskLevel, ToolResult
from tests.support import isolated_test_database

FOLLOWUPS = target.MISSING_MEDIA_FOLLOWUPS_KEY


def plan_fixture():
    return EffectPlan(
        plan_id="capacity-ticket", owner="capacity-owner", session_id="capacity-session",
        generation=7, owner_generation=11, tool_name="strm.run_once", effect=ToolEffect.DANGER,
        arguments={"nested": [1]}, preview={"nested": [2]}, expires_at=100,
        snapshot_fingerprint="snapshot", metadata={FOLLOWUPS: [{"id": "follow-1"}], "risk": " danger "},
        confirmation_contract={"audit_contract": {"version": 1, "action": "同步", "nested": [3]}},
    )


def normalize(value: object) -> object:
    if isinstance(value, ToolResult):
        return normalize(value.to_dict())
    if isinstance(value, dict):
        return {key: normalize(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize(child) for child in value]
    return value


def invoke(lifecycle, terminal, plan, value):
    if terminal == "completed":
        return lifecycle.completed(plan=plan, value=value, elapsed_ms=23)
    if terminal == "failed":
        return lifecycle.failed(plan=plan, code="precondition_failed", elapsed_ms=23)
    if terminal == "prepare_failed":
        return lifecycle.prepare_failed(
            prepared=PreparedEffect(preview={}, snapshot_fingerprint="s", metadata=plan.metadata),
            context=SimpleNamespace(owner="preparation-owner"),
        )
    return getattr(lifecycle, terminal)(plan=plan)


def exception_record(exc):
    return {"type": type(exc).__qualname__, "message": str(exc),
            "cause": exception_record(exc.__cause__) if exc.__cause__ is not None else None,
            "suppress_context": exc.__suppress_context__}


def capture_hook(terminal, fault):
    trace = []
    plan = plan_fixture()
    original = deepcopy(plan)
    value = ToolResult(True, "completed", "完成", data={"accepted": True})
    sentinel = RuntimeError("auxiliary failure")
    cause = ValueError("root cause")
    sentinel.__cause__ = cause
    cancelled = asyncio.CancelledError("task cancelled")

    def emit(name, **kwargs):
        if name in {"release", "complete"}:
            assert kwargs["owner"] == ("preparation-owner" if terminal == "prepare_failed" else plan.owner)
            assert kwargs["followups"] is plan.metadata[FOLLOWUPS]
        if name.startswith("audit-"):
            assert kwargs["owner"] == plan.owner
            assert kwargs["tool_name"] == plan.tool_name
            assert kwargs["risk"] is RiskLevel.DANGER
            assert kwargs["confirmation_id"] == plan.plan_id
            assert kwargs["owner_generation"] == 11
            assert kwargs["confirmation_contract"] == {"version": 1, "action": "同步", "nested": [3]}
            if terminal != "interrupted":
                assert kwargs["elapsed_ms"] == 23
            if terminal == "completed":
                assert kwargs["result"] is value
            elif terminal == "failed":
                assert kwargs["code"] == "precondition_failed"
        trace.append([name, normalize(kwargs)])
        if fault == name:
            raise sentinel
        if fault == "cancel:" + name:
            raise cancelled

    class Runtime:
        def release_followups(self, **kwargs):
            emit("release", **kwargs)
        def complete_submission(self, **kwargs):
            emit("complete", **kwargs)

    class Lifecycle(target.MediaFluxEffectLifecycle):
        # Preserve independent public override boundaries. Failed/interrupted
        # must NOT route their cleanup through the public cancelled hook.
        def cancelled(self, *, plan):
            if terminal != "cancelled":
                raise AssertionError("public cancelled override was crossed")
            return super().cancelled(plan=plan)
        def _risk(self, plan):
            emit("risk")
            return super()._risk(plan)
        def _contract(self, plan):
            emit("contract")
            return super()._contract(plan)

    lifecycle = Lifecycle(missing_media_runtime=cast(MissingMediaWorkflowRuntime | None, None if fault == "no-runtime" else Runtime()))
    with ExitStack() as stack:
        for function, name in (("record_confirmed_result", "audit-completed"),
                               ("record_confirmation_error", "audit-failed"),
                               ("record_confirmation_interrupted", "audit-interrupted")):
            stack.enter_context(patch.object(target, function, side_effect=lambda name=name, **kw: emit(name, **kw)))
        stack.enter_context(patch.object(target, "preference_undo_receipt", side_effect=lambda *a, **kw: emit("undo")))
        stack.enter_context(patch.object(target, "invalidate_agent_runtime_generation", side_effect=lambda: emit("invalidate")))
        stack.enter_context(patch.object(lifecycle, "_configuration_fingerprint", side_effect=lambda: (emit("fingerprint"), "new")[1]))
        try:
            returned = invoke(lifecycle, terminal, plan, value)
            outcome = {"returned": returned}
        except BaseException as exc:
            assert exc is sentinel or exc is cancelled
            assert exc.__cause__ is (cause if exc is sentinel else None)
            outcome = {"error": exception_record(exc)}
    assert plan == original
    normalized = normalize(value)
    assert isinstance(normalized, dict)
    return {"trace": trace, "outcome": outcome, "value": normalized}


@pytest.mark.parametrize("terminal", ["prepare_failed", "completed", "failed", "interrupted", "cancelled"])
@pytest.mark.parametrize("fault", ["none", "no-runtime", "release", "cancel:release"])
def test_hook_order_return_exception_and_identity(terminal, fault):
    result = capture_hook(terminal, fault)
    names = [item[0] for item in result["trace"]]
    if terminal == "completed":
        expected = [] if fault == "no-runtime" else ["complete"]
        assert names == expected + ["undo", "risk", "contract", "audit-completed", "fingerprint", "invalidate"]
        assert result["outcome"] == {"returned": None}
    elif fault in {"release", "cancel:release"}:
        assert names == ["release"]
        assert result["outcome"]["error"]["type"] == ("RuntimeError" if fault == "release" else "CancelledError")
    else:
        expected = [] if fault == "no-runtime" else ["release"]
        if terminal in {"failed", "interrupted"}:
            expected += ["risk", "contract", "audit-" + terminal]
        assert names == expected
        assert result["outcome"] == {"returned": None}


@pytest.mark.parametrize("terminal", ["completed", "failed", "interrupted"])
@pytest.mark.parametrize("fault", ["risk", "contract"])
def test_audit_argument_failure_stops_before_persistence(terminal, fault):
    result = capture_hook(terminal, fault)
    assert result["trace"][-1][0] == fault
    assert result["outcome"]["error"] == {
        "type": "RuntimeError", "message": "auxiliary failure", "suppress_context": True,
        "cause": {"type": "ValueError", "message": "root cause", "cause": None, "suppress_context": False},
    }


def test_followup_and_contract_clone_boundaries_are_not_deepened():
    plan = plan_fixture()
    seen = []
    class Runtime:
        def release_followups(self, *, owner, followups):
            assert followups is plan.metadata[FOLLOWUPS]
            seen.append(owner)
    lifecycle = target.MediaFluxEffectLifecycle(missing_media_runtime=cast(MissingMediaWorkflowRuntime, Runtime()))
    def audit(**kwargs):
        assert kwargs["confirmation_contract"] is not plan.confirmation_contract["audit_contract"]
        assert kwargs["confirmation_contract"]["nested"] is plan.confirmation_contract["audit_contract"]["nested"]
        assert kwargs["owner_generation"] == 11
        assert kwargs["risk"] is RiskLevel.DANGER
        seen.append("audit")
    with patch.object(target, "record_confirmation_error", side_effect=audit):
        lifecycle.failed(plan=plan, code="precondition_failed", elapsed_ms=23)
    assert seen == ["capacity-owner", "audit"]


@pytest.mark.parametrize("terminal", ["completed", "failed", "interrupted"])
def test_real_sqlite_audit_is_scoped_and_finalizes_once(terminal):
    plan = plan_fixture()
    lifecycle = target.MediaFluxEffectLifecycle()
    with isolated_test_database("capacity-audit.db") as path:
        record_confirmation_claimed(owner=plan.owner, confirmation_id=plan.plan_id,
            owner_generation=plan.owner_generation, tool_name=plan.tool_name, risk=RiskLevel.DANGER,
            confirmation_contract={"version": 1, "action": "同步"})
        value = ToolResult(True, "completed", "完成", data={"accepted": True})
        with patch.object(target, "preference_undo_receipt"), \
                patch.object(lifecycle, "_configuration_fingerprint", return_value="fixed"):
            invoke(lifecycle, terminal, replace(plan, metadata={"configuration_fingerprint": "fixed"}), value)
        rows = db.list_agent_action_history(owner_digest=media_owner_digest(plan.owner), limit=10)
        assert len(rows) == 1
        assert rows[0]["status"] == {"completed": "completed", "failed": "confirmation_stale", "interrupted": "outcome_unknown"}[terminal]
        assert db.list_agent_action_history(owner_digest=media_owner_digest("other-owner"), limit=10) == []
        # Reopen the real SQLite path rather than accepting only the API result.
        import sqlite3
        with sqlite3.connect(path) as conn:
            conn.row_factory = sqlite3.Row
            persisted = conn.execute("SELECT * FROM agent_action_history").fetchall()
            assert len(persisted) == 1
            assert {key: persisted[0][key] for key in rows[0].keys()} == dict(rows[0])
            assert persisted[0]["confirmation_id"] == "capacity-ticket-11"
            assert persisted[0]["owner_digest"] == media_owner_digest(plan.owner)
        assert rows[0]["mode"] == "confirmed_action"
        assert rows[0]["tool_name"] == "strm.run_once"
        assert rows[0]["risk"] == "danger"
        assert rows[0]["ok"] == (terminal == "completed")
        assert rows[0]["elapsed_ms"] == (0 if terminal == "interrupted" else 23)
        assert rows[0]["error_code"] == {
            "completed": "", "failed": "confirmation_stale", "interrupted": "execution_interrupted",
        }[terminal]
        assert rows[0]["safe_details"] == ('{"accepted":true}' if terminal == "completed" else '{}')
        assert rows[0]["summary"] == {
            "completed": "STRM 手动同步：已完成", "failed": "STRM 手动同步：确认已失效",
            "interrupted": "STRM 手动同步：结果待核对",
        }[terminal]


@pytest.mark.parametrize("fault", ["complete", "cancel:complete", "undo", "audit-completed", "fingerprint", "invalidate"])
def test_completion_failure_boundaries(fault):
    result = capture_hook("completed", fault)
    names = [item[0] for item in result["trace"]]
    if fault == "undo":
        assert result["outcome"] == {"returned": None}
        assert names[-1] == "invalidate"
        assert result["value"]["suggestions"] == ["本次操作未生成回退凭证；需要恢复时请先检查当前状态。"]
    else:
        assert names[-1] == fault.removeprefix("cancel:")
        assert result["outcome"]["error"]["type"] == ("CancelledError" if fault.startswith("cancel:") else "RuntimeError")


def test_plain_qa_keeps_one_model_zero_tool_nine_write_transactions():
    from contextlib import contextmanager

    from app.agent.kernel.capabilities import ToolCatalog
    from app.agent.kernel.model import ModelEvent, ModelEventType
    from app.agent.kernel.persistence import SQLiteKernelStore
    from app.agent.kernel.pipeline import ToolPipeline
    from app.agent.kernel.session import AgentSession
    from app.agent.kernel.state_types import AgentInput

    async def check():
        writes, read_changes = [], []
        class Model:
            count = 0
            async def stream(self, request, *, cancellation):
                self.count += 1
                yield ModelEvent(ModelEventType.TEXT_DELTA, text="离线回答。")
                yield ModelEvent(ModelEventType.FINISH, finish_reason="stop")
        model = Model()
        catalog = ToolCatalog([])
        store = SQLiteKernelStore(secret_provider=lambda: "capacity-synthetic-secret")
        lifecycle = target.MediaFluxEffectLifecycle()
        pipeline = ToolPipeline(catalog=catalog, state_store=store, reference_store=store, effect_lifecycle=lifecycle)
        session = AgentSession(model=model, catalog=catalog, pipeline=pipeline, state_store=store,
            journal=store,  system_prompt="Offline QA control")
        original = db.get_conn
        @contextmanager
        def traced():
            with original() as conn:
                begins, changes_before = [], conn.total_changes
                conn.set_trace_callback(lambda sql: begins.append(sql) if sql.startswith("BEGIN") else None)
                try:
                    yield conn
                finally:
                    conn.set_trace_callback(None)
                    if begins == ["BEGIN"]:
                        # 发布权检查在一致读事务中执行；不能把它算成写事务，或允许其偷偷写入。
                        read_changes.append(conn.total_changes - changes_before)
                    else:
                        writes.extend(begins)
        with isolated_test_database("capacity-qa.db"), patch.object(db, "get_conn", traced):
            events = [event async for event in session.run(AgentInput(
                message="你好", owner="capacity-qa", session_id="qa", request_id="request",
            ))]
            history = await store.list_events(owner="capacity-qa", session_id="qa")
            loaded = await store.load(owner="capacity-qa", session_id="qa")
        tool_count = sum(event.type.value == "tool.started" for event in events)
        assert (model.count, tool_count, len(writes)) == (1, 0, 9)  # Includes durable publication finish.
        assert writes == ["BEGIN IMMEDIATE"] * 9
        assert read_changes and set(read_changes) == {0}
        assert [[event.type.value, event.sequence, dict(event.payload)] for event in events] == [
            ["turn.started", 1, {"channel": "api", "generation": 1, "stream_display_enabled": True}],
            ["capabilities.selected", 2, {"count": 0, "tools": []}],
            ["model.started", 3, {"phase": "planning", "round": 1}],
            ["model.delta", 4, {"delta": "离线回答。", "round": 1}],
            ["turn.completed", 5, {"answer": "离线回答。", "finish_reason": "stop",
                                   "model_calls": 1, "status": "success", "tool_calls": 0, "usage": {},
                                   "business_tool_calls": 0, "discovery_tool_calls": 0, "successful_read_queries": 0,
                                   "turn_status": "success", "task": None}],
        ]
        user_event_id = loaded.conversation[0]["history_id"]
        assert isinstance(user_event_id, str) and user_event_id
        assert loaded.conversation == [
            {"content": "你好", "role": "user", "history_id": user_event_id, "history_order": 0},
            {"content": "离线回答。", "role": "assistant"},
        ]
        assert [row["type"] for row in history] == [event.type.value for event in events]
    asyncio.run(check())


@pytest.mark.parametrize("terminal", ["prepare_failed", "failed", "interrupted", "cancelled"])
def test_absent_missing_media_runtime_skips_followups_but_keeps_cloud_preview_cleanup(terminal):
    from collections.abc import Mapping

    class Metadata(Mapping):
        def __getitem__(self, key):
            if key == "source_kind":
                return "native_cloud_change"
            if key == "fs_change_plan_id":
                return "private-preview"
            raise AssertionError("unrelated followup metadata accessed without runtime")
        def __iter__(self):
            raise AssertionError("followup metadata copied without runtime")
        def __len__(self):
            raise AssertionError("followup metadata measured without runtime")

    class Lifecycle(target.MediaFluxEffectLifecycle):
        def _risk(self, plan):
            return RiskLevel.DANGER

    with patch.object(target, "record_confirmation_error"), patch.object(target, "record_confirmation_interrupted"), \
            patch.object(target, "discard_fs_change_plan") as discard:
        invoke(Lifecycle(), terminal, replace(plan_fixture(), tool_name="cloud.change", metadata=Metadata()), None)
    if terminal in {"prepare_failed", "cancelled"}:
        discard.assert_called_once_with("private-preview", preview_only=True)
    else:
        # 执行失败/中断的结果可能未知，不得按未提交预览清掉原计划。
        discard.assert_not_called()


def test_cancelled_undo_is_not_an_optional_failure():
    result = capture_hook("completed", "cancel:undo")
    assert [item[0] for item in result["trace"]] == ["complete", "undo"]
    assert result["outcome"]["error"]["type"] == "CancelledError"
    assert result["value"]["suggestions"] == []
