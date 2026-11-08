"""候选业务容量独立于模型/展示预算，搜索到确认与后置生命周期不丢尾项。"""
from __future__ import annotations

import asyncio
import inspect
import json
import shutil
import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.agent.domains.library.missing_workflow_runtime import (
    MissingMediaWorkflowRuntime,
)
from tests.resource_presentation_test_harness import new_resource_search_id, safe_resource_snapshot
from app.agent.domains.resources.selection import (
    current_candidate_view,
)
from app.agent.kernel.events import AgentEventType
from app.agent.kernel.model import ModelEvent, ModelEventType, ModelToolCall
from app.agent.kernel.state_types import AgentInput
from app.agent.models import ToolReference, ToolResult
from app.agent.public_view import candidate_result_lines
from app.agent.transports import QueryEnvelope
from app.indexers.models import IndexerItem
from app.indexers.result_store import IndexerResultStore
from tests import test_agent_ux_backend as ux
from tests.support import _prepare_batch


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("候选完整性回归不允许访问网络")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture
def store(tmp_path):
    yield from inspect.unwrap(ux.store)(tmp_path)


def _resources(count):
    result = ToolResult(True, "success", f"找到 {count} 项资源", data={"items": [
        {"result_id": f"capacity-resource-{position:03d}", "title": f"Example {position:03d}",
         "site_id": "demo", "site_name": "Demo", "size_text": "1 GiB",
         "download_state": "ready", "download_kinds": ["magnet"]}
        for position in range(1, count + 1)
    ]})
    result.references.append(ToolReference("resource_candidates", safe_resource_snapshot(
        result, search_id=new_resource_search_id(),
    )))
    return result


@pytest.mark.parametrize("natural", [False, True])
@pytest.mark.parametrize("count", [20, 50])
def test_full_batch_is_frozen_in_one_plan_and_only_dispatched_once_after_confirmation(store, natural, count):
    class Model:
        def __init__(self):
            self.intent = {}
            self.calls = 0

        async def stream(self, request, *, cancellation):
            self.calls += 1
            if any("已确认操作的可信系统结果" in row.content for row in request.messages):
                yield ModelEvent(ModelEventType.TEXT_DELTA, text="整批已处理。")
                yield ModelEvent(ModelEventType.FINISH, finish_reason="stop")
                return
            yield ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall("batch", "resources.download", self.intent))
            yield ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")

    async def exercise():
        model = Model()
        with patch.object(ux, "_resources", side_effect=lambda **_: _resources(count)):
            session, pipeline, states = ux._runtime(store, model=model)
            view, context = await ux._publish_candidates(pipeline, states)
        snapshot = await store.resolve(view["ref"], owner=ux.OWNER, session_id=ux.SESSION, expected_kind="resource_candidates")
        from app.indexers.runtime import get_indexer_service
        for item in snapshot["candidates"]:
            get_indexer_service().result_store.restore(item["result_id"], IndexerItem(title=item["title"], site_id="demo", site_name="Demo",
                magnet="magnet:?xt=urn:btih:" + "a" * 40, download_state="ready", download_kinds=("magnet",)))
        positions = list(range(1, count + 1))
        assert [item["position"] for item in view["items"]] == positions
        assert await current_candidate_view(state=await states.load(owner=ux.OWNER, session_id=ux.SESSION), store=store) == view
        # 新搜索不能替换已持有引用的业务身份；仍按旧快照整批预检与确认。
        if natural:
            with patch.object(ux, "_resources", side_effect=lambda **_: _resources(1)):
                await pipeline.execute("fixture.resource_read", {"title": "Other"}, context=context)
        model.intent = await ux._download_intent(store, view, positions)
        message = f"把这 {count} 项全部推送光鸭"
        user = AgentInput(owner=ux.OWNER, session_id=ux.SESSION, message=message) if natural else QueryEnvelope(
            owner=ux.OWNER, session_id=ux.SESSION, message=message, selection=ux._selection(view, positions),
        ).to_agent_input()
        result = {"ok": True, "status": "accepted", "summary": "整批处理完毕", "error": "","target": "guangya",
                  "total": count, "succeeded": count, "failed": 0, "items": [
            {"result_id": f"capacity-resource-{position:03d}", "status": "submitted", "request_id": position, "succeeded": ["guangya"], "failed": []}
            for position in positions
        ]}
        with patch("app.indexers.downloads.preview_indexer_download_batch", side_effect=_prepare_batch) as prepare, \
             patch("app.indexers.downloads.submit_indexer_download_batch", return_value=result) as execute:
            events = await ux._events(session.run(user))
            approvals = [event for event in events if event.type is AgentEventType.EFFECT_APPROVAL_REQUIRED]
            assert len(approvals) == 1
            assert model.calls == int(natural)
            ids = [f"capacity-resource-{position:03d}" for position in positions]
            assert prepare.call_args.args[1] == ids
            assert approvals[0].payload["plan"]["preview"]["data"]["count"] == count
            execute.assert_not_called()
            plan_id = approvals[0].payload["plan"]["plan_id"]
            confirmed = await ux._events(session.confirm(owner=ux.OWNER, session_id=ux.SESSION, plan_id=plan_id))
            completed = next(event.payload["result"] for event in confirmed if event.type is AgentEventType.EFFECT_COMPLETED)
            assert (completed["data"]["total"], completed["data"]["succeeded"], completed["data"]["failed"]) == (count, count, 0)
            assert [item["request_id"] for item in completed["data"]["items"]] == positions
            assert execute.call_args.args[1] == ids
            assert [item["result_id"] for item in completed["data"]["items"]] == ids
            if not natural:
                assert [item["position"] for item in completed["data"]["items"]] == positions
            await ux._events(session.confirm(owner=ux.OWNER, session_id=ux.SESSION, plan_id=plan_id))
            execute.assert_called_once()
    asyncio.run(exercise())


def test_lifecycle_matches_tail_verification_by_id_after_receipt_reordering():
    repository, recent = Mock(), Mock()
    repository.select_candidate.return_value = SimpleNamespace(workflow_id="workflow-" + "a" * 24, item_id="tail-" + "b" * 24, revision=1)
    runtime = MissingMediaWorkflowRuntime(repository=repository, recent_download_store=recent)
    proof = {"title": "Example", "tmdb_id": "12345", "season": 1, "episode": 20, "as_of": "2026-10-15"}
    followups = runtime.stage_candidates(owner=ux.OWNER, candidates=[{} for _ in range(19)] + [{
        "result_id": "capacity-resource-020", "verification": proof, "candidate_title": "Example S01E20", "target": "guangya",
    }])
    assert len(followups) == 20 and followups[:19] == (None,) * 19
    items = [{"result_id": f"capacity-resource-{pos:03}", "position": pos, "title": f"Example {pos}",
              "target": "guangya", "status": "submitted", "request_id": pos} for pos in range(20, 0, -1)]
    with patch("app.agent.domains.library.missing_workflow_runtime.db.get_agent_download_verification",
               return_value={"owner": ux.OWNER, "targets": [{**proof, "library_name": ""}]}) as get_job:
        runtime.complete_submission(owner=ux.OWNER, tool_name="resources.download",
            result=ToolResult(True, "accepted", "done", data={"items": items}), followups=followups)
    assert recent.capture.call_count == 20
    assert [call.kwargs["result"].data["request_id"] for call in recent.capture.call_args_list] == list(range(20, 0, -1))
    get_job.assert_called_once_with(20)
    repository.attach_submission.assert_called_once_with(workflow_ref={"workflow_id": "workflow-" + "a" * 24, "item_id": "tail-" + "b" * 24, "revision": 1},
                                                       request_id=20, verification_enqueued=True)
    assert len(candidate_result_lines({"source_type": "resource_candidates", "items": items})) == 20


def test_lifecycle_failure_returns_full_aligned_empty_slots():
    repository = Mock()
    repository.select_candidate.side_effect = RuntimeError("isolated repository failure")
    runtime = MissingMediaWorkflowRuntime(repository=repository, recent_download_store=Mock())
    candidates = [{} for _ in range(49)] + [{"verification": {"title": "Example"}}]
    assert runtime.stage_candidates(owner=ux.OWNER, candidates=candidates) == (None,) * 50
    repository.select_candidate.assert_called_once()


def test_multi_work_search_keeps_full_candidates_and_every_real_download_target():
    from app.modules.missing_resources import search_missing
    from tests.test_missing_resources import _audit_result, _search_result

    names = [f"CapacityShow{index:02d}" for index in range(12)]
    from dataclasses import asdict

    from tests.test_agent_indexer_actions import FakeIndexerService
    provider = FakeIndexerService(result_store=IndexerResultStore())
    items = [
        {"query": name, "season": 1, "episodes": [1, 2, 3]} for name in names
    ]

    def audit(arguments, **_kwargs):
        result = _audit_result(missing=[{"season": 1, "episode": episode} for episode in (1, 2, 3)])
        result.data.update(title=arguments["query"], tmdb_id=str(1000 + names.index(arguments["query"])))
        return result

    def search(request, site_ids=None, **_kwargs):
        arguments = asdict(request)
        name = arguments["title"]
        assert name in names
        assert arguments["season"] == 1 and arguments["episode"] is None
        # 单季共享查询返回完整候选池，不能继续按旧逐集协议格式化 episode=None。
        titles = [f"{name}.S01E{episode:02d}.1080p.WEB-DL" for episode in (1, 2, 3)]
        titles += ["UnrelatedShow.S01E01.1080p.WEB-DL",
                   f"{name}.S02E01.1080p.WEB-DL", f"{name}.S01E04.1080p.WEB-DL"]
        items = []
        for offset, title in enumerate(titles, 1):
            item = IndexerItem(
                "demo", "Demo", title, download_state="ready", download_kinds=("magnet",),
                magnet=f"magnet:?xt=urn:btih:{names.index(name) * len(titles) + offset:040x}",
            )
            items.append({**item.to_public_dict(), "result_id": provider.result_store.put(item)})
        return _search_result(query=f"{name} S01", items=items)

    with patch("app.modules.missing_resources.get_indexer_service", return_value=provider), \
         patch("app.modules.missing_resources.config.get_bool", return_value=True), \
         patch("app.modules.missing_resources.audit_series_episodes", side_effect=audit) as audits, \
         patch.object(provider, "search_media", new_callable=AsyncMock, side_effect=search) as searches:
        result = search_missing(items, owner="capacity-owner", as_of="2026-10-15")
    assert audits.call_count == searches.call_count == len(names)
    assert {call.args[0]["query"] for call in audits.call_args_list} == set(names)
    assert all(call.kwargs == {"refresh": True} for call in audits.call_args_list)
    assert {call.args[0].title for call in searches.call_args_list} == set(names)
    assert result["ok"] and result["status"] == "success"
    assert len(result["result_ids"]) == len(result["verification_targets"]) == 36
    assert len(result["items"]) == 72
    expected={(name,episode) for name in names for episode in (1,2,3)}
    assert {(p["query"],p["episode"]) for p in result["verification_targets"]} == expected
    for proof in result["verification_targets"]:
        assert proof["season"] == 1
        assert provider.result_store.get(proof["result_id"]).title == f"{proof['query']}.S01E{proof['episode']:02d}.1080p.WEB-DL"
    assert all(group["processed"]==3 and group["remaining"]==0 for group in result["groups"])


def test_web_candidate_data_filter_keeps_tail_without_any_visual_changes():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node 不可用，不能执行现有 Web 候选过滤逻辑")
    source = Path("app/static/js/agent.js").read_text()
    body = source.split("function renderCandidateView(turn, view) {", 1)[1].split("const previousGroup", 1)[0]
    javascript = f"""
const pick = new Function('turn', 'view', {json.dumps(body + 'return items;')});
const items = Array.from({{length: 50}}, (_, i) => ({{title: String(i + 1), position: i + 1}}));
const result = pick({{}}, {{ref: 'ref_capacity', expires_at: 1, items: [...items, {{title: 'bad', position: 0}}]}});
if (JSON.stringify(result.map(item => item.position)) !== JSON.stringify(items.map(item => item.position))) process.exit(1);
"""
    completed = subprocess.run([node, "--input-type=module", "-e", javascript], text=True, capture_output=True, check=False)
    assert completed.returncode == 0, completed.stderr
