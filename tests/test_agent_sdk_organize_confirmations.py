"""整理候选 SDK 的宿主确认契约；公共服务用替身，不执行文件操作或模型调用。"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app import sdk
from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.errors import ToolPipelineError
from app.agent.kernel import sandbox
from app.agent.kernel.bootstrap import build_tool_catalog
from app.agent.kernel.capabilities import ToolCatalog, ToolEffect
from app.agent.kernel.effects import ConfirmationEffectPlanStore, EffectExecutor
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.ports.mediaflux_policy import MediaFluxAuthorizationPolicy
from app.agent.kernel.ports.sdk_effects import activity_confirm_organize_tool
from app.agent.kernel.state_types import CancellationToken
from app.agent.kernel.tool_calls import ToolCallContext
from tests.support import initialized_web_runtime


OWNER = "webk:v1:" + "a" * 64
METHOD = "activity.confirm_organize"


@pytest.fixture
def runtime(monkeypatch):
    with initialized_web_runtime():
        monkeypatch.setattr("socket.socket.connect", Mock(side_effect=AssertionError("禁止外部服务")))
        yield


@pytest.fixture
def batches(monkeypatch):
    import app.modules

    entries = [{"confirmation_id": f"case-{i:04d}", "candidate_index": i % 3} for i in range(100)]
    frozen = {"entries": entries, "digest": "frozen-choice-digest", "items": [
        {**entry, "identity": f"媒体 {i}", "directory": f"/source/{i}",
         "candidate": {"title": f"候选 {i}-{entry['candidate_index']}", "tmdb_id": str(1000 + i),
                       "index": entry["candidate_index"]},
         "files": [{"file_id": f"file-{i}", "parent_id": f"source-{i}", "name": f"movie-{i}.mkv"}],
         "source": {"id": f"source-{i}", "target_id": "library"}, "rules": {"movie_dir": "library"}}
        for i, entry in enumerate(entries)]}
    api = SimpleNamespace(
        preview_choices=Mock(return_value=frozen),
        submit_choices=Mock(return_value={"items": [{**entry, "status": "queued", "error": ""} for entry in entries],
                                         "accepted": 100, "failed": 0}),
        list_confirmations=Mock(return_value={"items": [], "total": 0, "pages": 0, "page": 1,
                                             "confirmation_ids": [], "counts": {}, "selection_truncated": False,
                                             "review_enabled": False}),
        request_reviews=Mock(return_value={"requested": 100, "unchanged": 0}),
        frozen=frozen,
    )
    module_name = "app.modules.organize_confirmation_batches"
    module = ModuleType(module_name)
    for name in ("preview_choices", "submit_choices", "list_confirmations", "request_reviews"):
        setattr(module, name, getattr(api, name))
    monkeypatch.setitem(sys.modules, module_name, module)
    monkeypatch.setattr(app.modules, "organize_confirmation_batches", module, raising=False)
    return api


async def context_for(store, *, owner=OWNER, session_id="organize-confirmations"):
    lease, _ = await store.begin_turn(owner=owner, session_id=session_id, request_id="choose-candidates")
    return ToolCallContext(owner=owner, session_id=session_id, request_id=lease.request_id,
        turn_id=lease.turn_id, lease=lease, cancellation=CancellationToken(), report_progress=AsyncMock())


def executor(store, *, cls=EffectExecutor):
    return cls(catalog=ToolCatalog([activity_confirm_organize_tool()]), state_store=store,
        reference_store=store, authorization=MediaFluxAuthorizationPolicy(),
        effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore()))


def test_sdk_declares_effects_and_preserves_admin_activity_read_scope(batches):
    assert sdk.METHOD_EFFECTS[METHOD] == "write"
    assert sdk.METHOD_EFFECTS["activity.organize_confirmations"] == "read"
    assert sdk.METHOD_EFFECTS["activity.review_organize_confirmations"] == "control"
    catalog = build_tool_catalog()
    assert catalog.get(METHOD).effect is ToolEffect.DANGER
    registered = {tool.name for tool in catalog.visible() if tool.effect is not ToolEffect.READ}
    assert registered | {"session.batch"} == sdk.WRITE_METHODS
    confirmation_ids = ["case-0001", "case-0050"]
    with sdk.call_owner(OWNER):
        result = sdk.activity.organize_confirmations(batch_id="batch", task_id="task", source="guangya",
            reason="ambiguous", q="媒体", status="all", confirmation_ids=confirmation_ids, page=2, page_size=5)
        assert result is batches.list_confirmations.return_value
        assert sdk.activity.review_organize_confirmations(confirmation_ids) is batches.request_reviews.return_value
    batches.list_confirmations.assert_called_once_with(batch_id="batch", task_id="task", source="guangya",
        reason="ambiguous", q="媒体", status="all", confirmation_ids=confirmation_ids, page=2, page_size=5)
    batches.request_reviews.assert_called_once_with(confirmation_ids)
    batches.preview_choices.assert_not_called()
    batches.submit_choices.assert_not_called()
    with pytest.raises(TypeError):
        sdk.METHODS["activity.organize_confirmations"](chat_id="another-chat")
    with pytest.raises(ValueError, match="EffectExecutor"):
        sdk.activity.confirm_organize(batches.frozen["entries"])
    batches.submit_choices.assert_not_called()


def test_file_rpc_freezes_one_complete_plan_cold_confirm_submits_each_choice_once(runtime, batches, tmp_path, monkeypatch):
    monkeypatch.setattr("app.agent.effect_completion.wait_for_effect_completion", AsyncMock(side_effect=AssertionError("无集合 tracker")))
    monkeypatch.setattr("app.agent.effect_completion._register_effect_wait", AsyncMock(side_effect=AssertionError("无集合 tracker")))

    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store)
        (tmp_path / "run").mkdir()
        bridge = sandbox._SDKRequests(context, tmp_path, executor(store))
        bridge.uid = os.getuid()
        frozen = deepcopy(batches.frozen)
        request = tmp_path / "run/mf-requests/choices.json"
        request.write_text(json.dumps({"method": METHOD, "arguments": {"entries": frozen["entries"]}}))
        try:
            await bridge.dispatch(request.name)
            assert bridge.proposal is not None and bridge.proposal.effect_plan is not None
            plan = bridge.proposal.effect_plan
        finally:
            await bridge.close()
        assert plan.tool_name == METHOD
        assert plan.arguments == {"entries": frozen["entries"]}
        assert plan.metadata["organize_confirmations_preview"] == frozen
        assert plan.preview["data"]["items"] == frozen["items"]
        assert len(plan.preview["data"]["items"]) == 100
        assert len({row["title"] for row in plan.preview["data"]["resources"]}) == 100
        batches.submit_choices.assert_not_called()
        # 原服务对象变化不应修改已经落盘的完整预览。
        batches.frozen["items"][-1]["rules"]["movie_dir"] = "changed"
        restored = executor(SQLiteKernelStore())
        pending = await restored.get_pending_plan(owner=OWNER, session_id=context.session_id)
        assert pending is not None and pending.plan_id == plan.plan_id
        assert pending.metadata["organize_confirmations_preview"] == frozen
        outcome = await restored.execute_confirmed(plan.plan_id, context=context)
        result = outcome.outcome.public_content
        assert result["ok"] and result["status"] == "accepted"
        assert "尚未完成" in result["summary"]
        assert result["data"]["accepted"] == 100 and result["data"]["failed"] == 0
        assert [row["candidate_index"] for row in result["data"]["items"]] == [i % 3 for i in range(100)]
        assert all(row["status"] == "queued" for row in result["data"]["items"])
        batches.preview_choices.assert_called_once_with(frozen["entries"])
        batches.submit_choices.assert_called_once_with(frozen["entries"], frozen["digest"], actor="agent")
        with pytest.raises(ToolPipelineError):
            await restored.execute_confirmed(plan.plan_id, context=context)
        assert batches.submit_choices.call_count == 1

    asyncio.run(check())


@pytest.mark.parametrize("change", ["owner", "session", "arguments", "tool", "metadata", "fingerprint", "entries"])
def test_execution_rejects_wrong_frozen_scope_before_domain_submit(runtime, batches, change):
    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store)
        effects = executor(store)
        plan = (await effects.prepare(METHOD, {"entries": batches.frozen["entries"]}, context=context)).effect_plan
        assert plan is not None
        metadata = deepcopy(dict(plan.metadata))
        fingerprint = plan.snapshot_fingerprint
        if change == "owner":
            plan = replace(plan, owner="webk:v1:" + "b" * 64)
        elif change == "session":
            plan = replace(plan, session_id="foreign")
        elif change == "arguments":
            plan = replace(plan, arguments={"entries": [{"confirmation_id": "foreign", "candidate_index": 0}]})
        elif change == "tool":
            plan = replace(plan, tool_name="activity.reorganize")
        elif change == "metadata":
            metadata["organize_confirmations_preview"]["items"][-1]["files"][0]["name"] = "foreign.mkv"
            plan = replace(plan, metadata=metadata)
        elif change == "fingerprint":
            fingerprint = "foreign"
        else:
            # 即使原指纹仍有效，参数也不能脱离完整冻结选择。
            entries = deepcopy(plan.arguments["entries"])
            entries[-1]["candidate_index"] = 1
            plan = replace(plan, arguments={"entries": entries})
        tool = effects.catalog.get(METHOD)
        assert tool.execute_confirmed is not None
        with pytest.raises(ToolPipelineError) as error:
            tool.execute_confirmed({"entries": batches.frozen["entries"]} if change == "arguments" else dict(plan.arguments),
                fingerprint, replace(context, active_effect_plan=plan))
        assert error.value.code in {"confirmation_invalid", "confirmation_stale"}
        assert error.value.execution_started is False
        batches.submit_choices.assert_not_called()

    asyncio.run(check())


@pytest.mark.parametrize("case", ["cancel", "foreign_owner", "foreign_session", "runtime_change", "digest_change"])
def test_real_confirmation_fences_reject_cancelled_foreign_and_stale_plans(runtime, batches, case):
    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store)
        effects = executor(store)
        plan = (await effects.prepare(METHOD, {"entries": batches.frozen["entries"]}, context=context)).effect_plan
        assert plan is not None
        if case == "cancel":
            await effects.cancel_effect(plan.plan_id, lease=context.lease)
        elif case == "foreign_owner":
            context = await context_for(store, owner="webk:v1:" + "b" * 64)
        elif case == "foreign_session":
            context = await context_for(store, session_id="foreign")
        elif case == "runtime_change":
            from app import config
            config.set_and_save({"MEDIA_MOVIE_DIR": "changed-library"})
        else:
            batches.submit_choices.side_effect = ValueError("候选或文件规则摘要已变化")
        with pytest.raises(ToolPipelineError):
            await executor(SQLiteKernelStore()).execute_confirmed(plan.plan_id, context=context)
        if case == "digest_change":
            batches.submit_choices.assert_called_once_with(batches.frozen["entries"], batches.frozen["digest"], actor="agent")
        else:
            batches.submit_choices.assert_not_called()

    asyncio.run(check())


@pytest.mark.parametrize("entries", [[], [{"confirmation_id": "case", "candidate_index": True}],
    [{"confirmation_id": "case", "candidate_index": -1}], [{"confirmation_id": "case", "candidate_index": 0, "actor": "human"}]])
def test_write_schema_rejects_invalid_or_model_supplied_authority(runtime, batches, entries):
    async def check():
        store = SQLiteKernelStore()
        with pytest.raises(ToolPipelineError):
            await executor(store).prepare(METHOD, {"entries": entries}, context=await context_for(store))
        batches.preview_choices.assert_not_called()
        batches.submit_choices.assert_not_called()
    asyncio.run(check())


@pytest.mark.parametrize("accepted,failed", [(1, 1), (0, 2)])
def test_receipt_preserves_per_item_rejections_without_claiming_completion(runtime, batches, accepted, failed):
    entries = batches.frozen["entries"][:2]
    batches.frozen["entries"] = entries
    batches.frozen["items"] = batches.frozen["items"][:2]
    items = [{**entry, "status": "queued" if i < accepted else "failed",
              "error": "" if i < accepted else "已由其他确认选定"} for i, entry in enumerate(entries)]
    batches.submit_choices.return_value = {"accepted": accepted, "failed": failed, "items": items}

    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store)
        effects = executor(store)
        plan = (await effects.prepare(METHOD, {"entries": entries}, context=context)).effect_plan
        assert plan is not None
        result = (await effects.execute_confirmed(plan.plan_id, context=context)).outcome.public_content
        assert result["ok"] is bool(accepted)
        assert result["status"] == ("accepted" if accepted else "failed")
        assert result["data"]["items"] == items
        assert (result["data"]["accepted"], result["data"]["failed"]) == (accepted, failed)
        assert result["data"]["confirmation_ids"] == [entry["confirmation_id"] for entry in entries]
        assert "完成后会自动回报" not in result["summary"]

    asyncio.run(check())


def test_domain_transport_uncertainty_is_not_reclassified_as_unsubmitted(runtime, batches):
    batches.submit_choices.side_effect = RuntimeError("持久提交后连接中断")

    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store)
        effects = executor(store)
        plan = (await effects.prepare(METHOD, {"entries": batches.frozen["entries"]}, context=context)).effect_plan
        assert plan is not None
        with pytest.raises(ToolPipelineError) as error:
            await effects.execute_confirmed(plan.plan_id, context=context)
        assert error.value.code == "outcome_unknown" and error.value.execution_started
        with pytest.raises(ToolPipelineError):
            await effects.execute_confirmed(plan.plan_id, context=context)
        assert batches.submit_choices.call_count == 1
    asyncio.run(check())


def test_cancelled_host_context_never_submits(runtime, batches):
    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store)
        effects = executor(store)
        plan = (await effects.prepare(METHOD, {"entries": batches.frozen["entries"]}, context=context)).effect_plan
        assert plan is not None
        context.cancellation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await effects.execute_confirmed(plan.plan_id, context=context)
        batches.submit_choices.assert_not_called()
    asyncio.run(check())


def test_review_control_rpc_is_available_in_read_only_scope_without_confirmation(runtime, batches, tmp_path):
    async def check():
        store = SQLiteKernelStore()
        context = replace(await context_for(store), read_only=True)
        (tmp_path / "run").mkdir()
        bridge = sandbox._SDKRequests(context, tmp_path, executor(store))
        bridge.uid = os.getuid()
        confirmation_ids = [entry["confirmation_id"] for entry in batches.frozen["entries"]]
        request = tmp_path / "run/mf-requests/review.json"
        request.write_text(json.dumps({"method": "activity.review_organize_confirmations", "arguments": {"confirmation_ids": confirmation_ids}}))
        try:
            await bridge.dispatch(request.name)
            assert bridge.proposal is None
            response = json.loads((tmp_path / "run/mf-responses/review.json").read_text())
            assert response["result"] == {"requested": 100, "unchanged": 0}
        finally:
            await bridge.close()
        batches.request_reviews.assert_called_once_with(confirmation_ids)
        batches.preview_choices.assert_not_called()
        batches.submit_choices.assert_not_called()
    asyncio.run(check())


@pytest.fixture
def confirmation_backend(runtime, monkeypatch):
    from app import database as db
    from app.modules import organize_confirmations as confirmations
    from app.modules.organize import OrganizeRules, organize_rules_snapshot

    rules = OrganizeRules(target_dir_id="target")
    monkeypatch.setattr(OrganizeRules, "from_config", lambda: rules)
    monkeypatch.setattr(OrganizeRules, "for_source", lambda self, _source: rules)
    monkeypatch.setattr(confirmations, "_recognition_review_is_enabled", lambda: False)
    wake = Mock()
    monkeypatch.setattr(confirmations, "wake_confirmation_dispatcher", wake)
    dispatch = Mock(side_effect=AssertionError("仅入队，不执行云盘任务"))
    monkeypatch.setattr(confirmations, "_dispatch_confirmation_token", dispatch)
    for i in range(100):
        token = f"native-{i:04d}"
        payload = {"identity": f"作品{i}", "directory": f"/incoming/{i}", "source_dir_id": "source",
            "source_parent_id": "parent", "source_name": "SDK 来源", "_confirmation_batch": "sdk-batch",
            "files": [{"file_id": f"file-{i}", "name": f"movie-{i}.mkv", "parent_id": "parent", "size": 100}],
            "candidates": [{"title": f"作品{i}-候选{index}", "tmdb_id": str(100 + index), "year": "2025",
                            "provider": "tmdb", "media_type": "movie"} for index in range(2)],
            "rules": organize_rules_snapshot(rules), "review_only": True}
        db.create_organize_confirmation(token=token, fingerprint=confirmations._fingerprint(payload), chat_id=f"chat-{i % 2}",
            source_name="SDK 来源", directory_path=payload["directory"], payload=payload,
            expires_at=(datetime.now() + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S"))
    return SimpleNamespace(wake=wake, dispatch=dispatch)


def test_real_batch_service_queues_one_hundred_distinct_choices_as_explicit_agent_and_reads_progress(confirmation_backend):
    from app import database as db

    page = sdk.activity.organize_confirmations(batch_id="sdk-batch")
    assert (page["total"], page["pages"], len(page["confirmation_ids"]), len(page["items"])) == (100, 5, 100, 20)
    assert page["review_enabled"] is False
    entries = [{"confirmation_id": token, "candidate_index": int(token.rsplit("-", 1)[1]) % 2} for token in page["confirmation_ids"]]

    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store)
        effects = executor(store)
        plan = (await effects.prepare(METHOD, {"entries": entries}, context=context)).effect_plan
        assert plan is not None and len(plan.metadata["organize_confirmations_preview"]["items"]) == 100
        assert sdk.activity.organize_confirmations(confirmation_ids=page["confirmation_ids"])["counts"] == {"pending": 100}
        result = (await effects.execute_confirmed(plan.plan_id, context=context)).outcome.public_content
        assert result["status"] == "accepted" and result["data"]["accepted"] == 100
        progress = sdk.activity.organize_confirmations(confirmation_ids=page["confirmation_ids"], status="all", page_size=100)
        assert progress["counts"] == {"queued": 100}
        by_confirmation_id = {item["confirmation_id"]: item for item in progress["items"]}
        for entry in entries:
            item = by_confirmation_id[entry["confirmation_id"]]
            assert item["selected_index"] == entry["candidate_index"] and not item["error"]
            row = db.get_organize_confirmation(entry["confirmation_id"])
            assert row is not None and row["confirmation_actor"] == "agent"
            assert json.loads(row["payload_json"])["_explicit_approval"] is True
        confirmation_backend.wake.assert_called_once_with()
        confirmation_backend.dispatch.assert_not_called()
    asyncio.run(check())


@pytest.mark.skipif(not sandbox.sandbox_supported(), reason="需要真实 Linux 工作区隔离")
def test_run_code_read_review_pause_cold_confirm_queue_and_read_same_confirmation_ids(confirmation_backend, monkeypatch, tmp_path):
    from app import database as db
    from app.modules import agent_recognition_review as reviews, organize_confirmation_batches as batches
    from app.modules import organize_confirmations as confirmations

    monkeypatch.setattr(sandbox, "get_runtime_paths", lambda: SimpleNamespace(data_dir=tmp_path, runtime_dir=tmp_path / "runtime"))
    monkeypatch.setattr(confirmations, "_recognition_review_is_enabled", lambda: True)
    monkeypatch.setattr(confirmations, "wake_recognition_review_dispatcher", Mock())
    monkeypatch.setattr("app.modules.telegram_confirmation_batch.publish_batch_summary", Mock())
    model = Mock(side_effect=lambda payload: reviews.RecognitionReviewDecision(status="approved",
        candidate_index=int(payload["files"][0]["file_id"].rsplit("-", 1)[1]) % 2,
        confidence=.99, reason_code="verified_identity", summary="核对文件与候选身份一致"))
    monkeypatch.setattr(reviews, "review_confirmation_payload", model)
    submit = Mock(wraps=batches.submit_choices)
    start = Mock(wraps=confirmations.start_confirmation)
    monkeypatch.setattr(batches, "submit_choices", submit)
    monkeypatch.setattr(confirmations, "start_confirmation", start)
    expected_choices = [
        {"confirmation_id": item["confirmation_id"], "candidate_index": index,
         "title": item["candidates"][index]["title"]}
        for index, item in enumerate(sdk.activity.organize_confirmations(batch_id="sdk-batch", page_size=2)["items"])
    ]
    assert len({item["confirmation_id"] for item in expected_choices}) == 2

    def process_reviews(count):
        for _ in range(count):
            row = db.claim_next_organize_confirmation_review()
            assert row is not None
            assert confirmations._process_recognition_review_row(row) == "approved"

    async def check():
        store = SQLiteKernelStore()
        context = await context_for(store)
        pipeline = executor(store, cls=ToolPipeline)
        assert isinstance(pipeline, ToolPipeline)
        pipeline.catalog.register(sandbox.run_code_tool(pipeline))

        async def code(source):
            result = await pipeline.execute(sandbox.RUN_CODE_TOOL, {"code": source}, context=context)
            assert result.outcome.public_content["ok"] is True, result.outcome.public_content
            if result.effect_plan is None:
                assert result.outcome.public_content["data"]["exit_code"] == 0, result.outcome.public_content
            return result

        read = await code('''import json
from pathlib import Path
from mf import activity
try:
    from app import sdk as raw_sdk
except ModuleNotFoundError:
    pass
else:
    raise AssertionError("工作区不得导入宿主 SDK")
page = activity.organize_confirmations(batch_id="sdk-batch", page_size=1000)
assert page["total"] == len(page["confirmation_ids"]) == 100
choices = [{"confirmation_id": item["confirmation_id"], "candidate_index": index,
            "title": item["candidates"][index]["title"]} for index, item in enumerate(page["items"][:2])]
print(json.dumps({"confirmation_ids": [item["confirmation_id"] for item in choices], "items": choices,
                  "credentials": "sandbox-private-credential", "access_token": "sandbox-private-access"}, ensure_ascii=False))
Path("confirmation_ids.json").write_text(json.dumps(page["confirmation_ids"]))
requested = activity.review_organize_confirmations(page["confirmation_ids"])
assert requested["requested"] == 100
progress = activity.organize_confirmations(confirmation_ids=page["confirmation_ids"], status="all", page_size=1000)
assert progress["counts"] == {"pending": 100}
assert all(item["review_status"] == "pending" and item["suggestion"] is None for item in progress["items"])
''')
        assert read.effect_plan is None
        # 实际 SDK RPC -> 沙箱 stdout -> 公开/模型投影；不能仅检查沙箱内未脱敏的原数据。
        for content in (read.outcome.public_content, json.loads(read.outcome.model_content)):
            visible = json.loads(content["data"]["stdout"])
            assert visible["items"] == expected_choices
            assert visible["confirmation_ids"] == [item["confirmation_id"] for item in expected_choices]
            assert visible["credentials"] == visible["access_token"] == "********"
        assert [call["method"] for call in read.outcome.public_content["data"]["sdk_calls"]] == [
            "activity.organize_confirmations", "activity.review_organize_confirmations", "activity.organize_confirmations"]
        process_reviews(50)
        pending = await code('''import json
from pathlib import Path
from mf import activity
confirmation_ids = json.loads(Path("confirmation_ids.json").read_text())
progress = activity.organize_confirmations(confirmation_ids=confirmation_ids, status="all", page_size=1000)
assert progress["counts"] == {"pending": 100}
assert sum(item["review_status"] == "pending" for item in progress["items"]) == 50
assert sum(item["review_status"] == "approved" for item in progress["items"]) == 50
assert all(item["selected_index"] is None for item in progress["items"])
''')
        assert pending.effect_plan is None
        submit.assert_not_called()
        start.assert_not_called()
        confirmation_backend.wake.assert_not_called()
        process_reviews(50)
        prepared = await code('''import json
from pathlib import Path
from mf import activity
confirmation_ids = json.loads(Path("confirmation_ids.json").read_text())
page = activity.organize_confirmations(confirmation_ids=confirmation_ids, status="all", page_size=1000)
assert page["counts"] == {"pending": 100}
assert all(item["review_status"] == "approved" for item in page["items"])
entries = [{"confirmation_id": item["confirmation_id"], "candidate_index": item["suggestion"]["candidate_index"]} for item in page["items"]]
assert {entry["candidate_index"] for entry in entries} == {0, 1}
Path("entries.json").write_text(json.dumps(entries))
with Path("runs.txt").open("a") as stream:
    stream.write("once\\n")
activity.confirm_organize(entries)
Path("after-write.txt").write_text("must not run")
''')
        plan = prepared.effect_plan
        assert plan is not None and plan.tool_name == METHOD
        assert prepared.outcome.public_content["status"] == "approval_required"
        root, _ = sandbox._workspace_location(OWNER, context.session_id)
        entries = json.loads((root / "work/entries.json").read_text())
        confirmation_ids = json.loads((root / "work/confirmation_ids.json").read_text())
        assert plan.arguments == {"entries": entries}
        assert (root / "work/runs.txt").read_text() == "once\n"
        assert not (root / "work/after-write.txt").exists()
        submit.assert_not_called()
        start.assert_not_called()
        rebuilt = executor(SQLiteKernelStore())
        for foreign in (await context_for(store, owner="webk:v1:" + "b" * 64),
                        await context_for(store, session_id="foreign")):
            with pytest.raises(ToolPipelineError):
                await rebuilt.execute_confirmed(plan.plan_id, context=foreign)
        with pytest.raises(ToolPipelineError):
            await rebuilt.execute_confirmed("not-the-approved-plan", context=context)
        submit.assert_not_called()
        restored = await rebuilt.get_pending_plan(owner=OWNER, session_id=context.session_id)
        assert restored is not None and restored.plan_id == plan.plan_id
        result = (await rebuilt.execute_confirmed(plan.plan_id, context=context)).outcome.public_content
        assert result["status"] == "accepted" and result["data"]["accepted"] == 100
        submit.assert_called_once_with(entries, plan.metadata["organize_confirmations_preview"]["digest"], actor="agent")
        assert start.call_count == 100
        assert all(call.kwargs["actor"] == "agent" and call.kwargs["explicit_approval"] is True
                   and call.kwargs["dispatch"] is False for call in start.call_args_list)
        followed = await code('''import json
from pathlib import Path
from mf import activity
confirmation_ids = json.loads(Path("confirmation_ids.json").read_text())
entries = json.loads(Path("entries.json").read_text())
progress = activity.organize_confirmations(confirmation_ids=confirmation_ids, status="all", page_size=1000)
assert set(progress["confirmation_ids"]) == set(confirmation_ids)
assert progress["counts"] == {"queued": 100}
by_confirmation_id = {item["confirmation_id"]: item for item in progress["items"]}
assert all(by_confirmation_id[entry["confirmation_id"]]["selected_index"] == entry["candidate_index"] for entry in entries)
Path("progress.json").write_text(json.dumps(progress))
assert Path("runs.txt").read_text() == "once\\n"
assert not Path("after-write.txt").exists()
''')
        assert followed.effect_plan is None
        progress = json.loads((root / "work/progress.json").read_text())
        assert set(progress["confirmation_ids"]) == set(confirmation_ids)
        assert model.call_count == 100
        confirmation_backend.dispatch.assert_not_called()
        confirmation_backend.wake.assert_called_once_with()
        with pytest.raises(ToolPipelineError):
            await rebuilt.execute_confirmed(plan.plan_id, context=context)
        assert submit.call_count == 1
    asyncio.run(check())
