"""Agent UX 后端正式回归：只读入口、显示元数据与候选选择安全边界。"""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from app import database as db
from app.agent.domain_catalog import build_tool_specs
from app.agent.ingest_actions import AgentIngestSessionStore
from app.agent.kernel.capabilities import CapabilityRetriever
from app.agent.kernel.events import AgentEventType
from app.agent.kernel.model import ModelEvent, ModelEventType, ModelToolCall
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolCallContext, ToolPipeline, ToolPipelineError
from app.agent.kernel.ports.existing_actions import catalog_from_tool_specs
from app.agent.kernel.session import AgentSession
from app.agent.kernel.state import (
    AgentInput,
    CancellationToken,
    InMemorySessionStateStore,
    SelectionInvalidError,
    StateUpdate,
)
from app.agent.kernel.transports import QueryEnvelope
from app.agent.kernel.ux_selection import (
    CANDIDATE_VIEW_KEY,
    current_candidate_view,
    validate_selection,
)
from app.agent.models import ToolReference, ToolResult
from app.agent.recent_resource_candidates import (
    RecentResourceCandidateStore,
    new_resource_search_id,
    safe_resource_snapshot,
)
from app.routes import agent_api

SECRET = "agent-ux-backend-test-secret"
OWNER = "owner-ux"
SESSION = "session_ux_12345678"
CSRF = {"X-CSRF-Token": "ux-csrf"}


@pytest.fixture
def store(tmp_path):
    previous_path = db.DB_PATH
    previous_test_mode = bool(getattr(db, "_configured_test_mode", False))
    db.configure_database(tmp_path / "agent-ux.db", test_mode=True)
    db.init_db()
    try:
        yield SQLiteKernelStore(secret_provider=lambda: SECRET)
    finally:
        db.configure_database(previous_path, test_mode=previous_test_mode)


@pytest.fixture
def api(store, monkeypatch):
    from app.main import SecurityMiddleware

    application = FastAPI()
    application.include_router(agent_api.router)
    application.add_middleware(SecurityMiddleware)
    application.add_middleware(SessionMiddleware, secret_key=SECRET)

    @application.get("/_test_login")
    def login(request: Request):
        request.session.update(logged_in=True, csrf_token="ux-csrf")
        return {"ok": True}

    runtime = SimpleNamespace(store=store)
    principal = {"username": "ux-account-a"}
    monkeypatch.setattr(agent_api.config, "web_credentials", lambda: (principal["username"], "unused"))
    monkeypatch.setattr(agent_api, "get_agent_kernel_runtime", lambda: runtime)
    monkeypatch.setattr(agent_api, "is_agent_enabled", lambda: True)
    monkeypatch.setattr(agent_api, "get_web_secret", lambda: SECRET)
    monkeypatch.setattr(agent_api.agent_rate_limiter, "allow", lambda *a, **k: True)
    with TestClient(application, raise_server_exceptions=False) as client:
        yield SimpleNamespace(client=client, runtime=runtime, principal=principal, store=store)


def _login(api):
    assert api.client.get("/_test_login").status_code == 200


def _web_owner(api):
    return agent_api.web_kernel_owner(api.principal["username"])


async def _seed(store, *, owner=OWNER, session_id=SESSION):
    lease, _ = await store.begin_turn(owner=owner, session_id=session_id, request_id="seed")
    await store.commit(
        lease, conversation=[{"role": "user", "content": "原始标题"}],
        updates=(StateUpdate("pending_effect_plan_id", "existing-plan"),
                 StateUpdate("metadata.inflight", {"operation": "protected"})),
    )
    return lease


def _row_snapshot():
    with db.get_conn() as conn:
        return {
            table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table}").fetchall()]
            for table in ("agent_kernel_sessions", "agent_kernel_session_epochs", "agent_kernel_refs")
        }


def test_next_actions_login_feature_gate_rate_limit_and_no_runtime(api, monkeypatch):
    reader = Mock(return_value=ToolResult(True, "empty", "无待办", data={"snapshot_status": "empty"}))
    monkeypatch.setattr(agent_api, "summarize_workspace_next_actions", reader)
    runtime = Mock(side_effect=AssertionError("首页不能初始化 Kernel/模型"))
    monkeypatch.setattr(agent_api, "get_agent_kernel_runtime", runtime)
    assert api.client.get("/api/agent/next-actions").status_code == 401
    assert "draft_scope" not in api.client.get("/api/agent/sessions").text
    reader.assert_not_called()
    _login(api)
    monkeypatch.setattr(agent_api, "is_agent_enabled", lambda: False)
    assert api.client.get("/api/agent/next-actions").status_code == 400
    reader.assert_not_called()
    monkeypatch.setattr(agent_api, "is_agent_enabled", lambda: True)
    monkeypatch.setattr(agent_api.agent_rate_limiter, "allow", lambda *a, **k: False)
    assert api.client.get("/api/agent/next-actions").status_code == 429
    reader.assert_not_called()
    monkeypatch.setattr(agent_api.agent_rate_limiter, "allow", lambda *a, **k: True)
    response = api.client.get("/api/agent/next-actions")
    assert response.json() == {"actions": [], "snapshot_status": "empty"}
    assert "no-store" in response.headers["cache-control"]
    reader.assert_called_once_with({})
    runtime.assert_not_called()


def test_next_actions_existing_local_projection_max_three_and_unavailable(api, monkeypatch):
    _login(api)
    sources = [("downloads", "download_needs_review"), ("rss", "rss_failed"),
               ("organize", "organize_issue"), ("strm", "strm_open_failure")]
    todo = ToolResult(True, "attention", "待办", data={
        "areas": [{"source": source, "status": "attention", "attention_count": 1,
                   "reason_codes": [reason]} for source, reason in sources],
    })
    with patch("app.agent.workspace_next_actions.summarize_workspace_todo", return_value=todo) as reader:
        response = api.client.get("/api/agent/next-actions")
    assert response.status_code == 200
    payload = response.json()
    assert payload["snapshot_status"] == "attention"
    assert len(payload["actions"]) == 3
    assert [item["id"] for item in payload["actions"]] == ["review_downloads", "review_rss", "review_organize"]
    assert all(set(item) == {"id", "title", "description", "prompt"} for item in payload["actions"])
    reader.assert_called_once_with({})
    monkeypatch.setattr(agent_api, "summarize_workspace_next_actions", Mock(side_effect=RuntimeError("https://private/token")))
    failed = api.client.get("/api/agent/next-actions")
    assert failed.status_code == 200
    assert failed.json() == {"actions": [], "snapshot_status": "unavailable"}
    monkeypatch.setattr(agent_api, "summarize_workspace_next_actions", lambda _: ToolResult(False, "unavailable", "失败"))
    assert api.client.get("/api/agent/next-actions").json() == failed.json()


def test_next_actions_real_snapshot_never_calls_network_scans_or_writes(api):
    _login(api)
    asyncio.run(_seed(api.store))
    before = _row_snapshot()
    with patch("socket.socket.connect", side_effect=AssertionError("network forbidden")) as network, \
         patch("os.scandir", side_effect=AssertionError("scan forbidden")) as scan, \
         patch.object(agent_api, "get_agent_kernel_runtime", side_effect=AssertionError("kernel forbidden")) as kernel:
        response = api.client.get("/api/agent/next-actions")
    assert response.status_code == 200
    assert response.json()["snapshot_status"] != "unavailable", response.text
    network.assert_not_called()
    scan.assert_not_called()
    kernel.assert_not_called()
    assert _row_snapshot() == before


def test_patch_auth_csrf_owner_missing_clean_title_and_stable_draft_scope(api, monkeypatch):
    path = f"/api/agent/sessions/{SESSION}"
    assert api.client.patch(path, json={"title": "新标题"}, headers=CSRF).status_code == 401
    _login(api)
    owner = _web_owner(api)
    asyncio.run(_seed(api.store, owner=owner))
    before = _row_snapshot()
    assert api.client.patch(path, json={"title": "不应写入"}).status_code == 403
    assert _row_snapshot() == before
    response = api.client.patch(path, json={"title": "  <b>新\n 标题</b>\u200b ", "pinned": True}, headers=CSRF)
    assert response.status_code == 200, response.text
    summary = response.json()["session"]
    assert summary["title"] == "新 标题" and summary["pinned"] is True
    assert summary["generation"] == 1 and summary["pending_approval"] is True
    response = api.client.get("/api/agent/sessions")
    assert response.json()["sessions"][0] == summary
    assert response.json()["scope"] == "recent_sessions"
    scope = response.json()["draft_scope"]
    assert len(scope) == 64 and all(char in "0123456789abcdef" for char in scope)
    assert api.principal["username"] not in scope and owner not in scope
    _login(api)
    assert api.client.get("/api/agent/sessions").json()["draft_scope"] == scope
    before = _row_snapshot()
    api.principal["username"] = "ux-account-b"
    assert api.client.patch(path, json={"pinned": False}, headers=CSRF).status_code == 404
    assert api.client.get("/api/agent/sessions").json()["sessions"] == []
    assert api.client.get("/api/agent/sessions").json()["draft_scope"] != scope
    assert api.client.patch("/api/agent/sessions/missing_1234567890", json={"title": "不存在"}, headers=CSRF).status_code == 404
    assert _row_snapshot() == before
    api.principal["username"] = "ux-account-a"
    monkeypatch.setattr(agent_api, "get_web_secret", lambda: SECRET + "rotated")
    assert api.client.get("/api/agent/sessions").json()["draft_scope"] != scope


@pytest.mark.parametrize("payload", [None, {}, {"title": 42}, {"title": " "}, {"title": "a" * 81},
    {"title": "https://internal/private"}, {"pinned": 1}, {"pinned": "true"},
    {"title": "有效", "conversation": []}, {"generation": 1}, {"title": "\u0000\u200b"}])
def test_patch_strict_validation_has_no_side_effect(api, payload):
    _login(api)
    asyncio.run(_seed(api.store, owner=_web_owner(api)))
    before = _row_snapshot()
    response = api.client.patch(f"/api/agent/sessions/{SESSION}", json=payload, headers=CSRF)
    assert response.status_code == 400, response.text
    assert _row_snapshot() == before


def test_metadata_patch_keeps_inflight_pending_generation_and_concurrent_turn(store):
    async def exercise():
        lease = await _seed(store)
        before = await store.load(owner=OWNER, session_id=SESSION)
        rows_before = _row_snapshot()
        second_store = SQLiteKernelStore(secret_provider=lambda: SECRET)
        await store.patch_session_display(owner=OWNER, session_id=SESSION, patch={"title": "用户标题"})
        after = await second_store.load(owner=OWNER, session_id=SESSION)
        assert after.generation == before.generation
        assert after.conversation == before.conversation
        assert after.pending_effect_plan_id == before.pending_effect_plan_id
        assert after.metadata["inflight"] == before.metadata["inflight"]
        assert _row_snapshot()["agent_kernel_session_epochs"] == rows_before["agent_kernel_session_epochs"]
        assert _row_snapshot()["agent_kernel_sessions"][0][-1] == rows_before["agent_kernel_sessions"][0][-1]
        await asyncio.gather(
            second_store.commit(lease, conversation=[{"role": "user", "content": "新回合内容"}],
                                updates=(StateUpdate("metadata.new_turn", True),)),
            store.patch_session_display(owner=OWNER, session_id=SESSION, patch={"pinned": True}),
        )
        state = await second_store.load(owner=OWNER, session_id=SESSION)
        assert state.conversation == [{"role": "user", "content": "新回合内容"}]
        assert state.metadata == {"inflight": {"operation": "protected"}, "title": "用户标题", "pinned": True, "new_turn": True}
        assert state.generation == lease.generation and state.pending_effect_plan_id == "existing-plan"
    asyncio.run(exercise())


def _patch_in_other_process(path, ready, proceed, result):
    db.configure_database(path, test_mode=True)
    local = SQLiteKernelStore(secret_provider=lambda: SECRET)
    # 模拟另一进程已持有旧状态，但 PATCH 必须在事务内读最新状态。
    asyncio.run(local.load(owner=OWNER, session_id=SESSION))
    ready.set()
    if not proceed.wait(10):
        result.put("timeout")
        return
    result.put(asyncio.run(local.patch_session_display(owner=OWNER, session_id=SESSION, patch={"title": "跨进程标题"})))


def test_cross_process_patch_cannot_overwrite_a_new_generation(store):
    asyncio.run(_seed(store))
    context = multiprocessing.get_context("spawn")
    ready, proceed, result = context.Event(), context.Event(), context.Queue()
    process = context.Process(target=_patch_in_other_process, args=(str(db.DB_PATH), ready, proceed, result))
    process.start()
    try:
        assert ready.wait(10)
        async def advance():
            lease, _ = await store.begin_turn(owner=OWNER, session_id=SESSION, request_id="newer")
            return await store.commit(lease, conversation=[{"role": "user", "content": "不可丢失的新回合"}])
        latest = asyncio.run(advance())
        proceed.set()
        assert result.get(timeout=10)["generation"] == latest.generation
        process.join(10)
        assert process.exitcode == 0
        after = asyncio.run(store.load(owner=OWNER, session_id=SESSION))
        assert after.conversation == latest.conversation
        assert after.generation == latest.generation
        assert after.metadata["title"] == "跨进程标题"
        assert after.pending_effect_plan_id == latest.pending_effect_plan_id
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)
        result.close()


def test_pinned_older_than_recent_fifty_is_visible_and_corrupt_rows_do_not_hide_it(store):
    async def exercise():
        for index in range(62):
            await _seed(store, session_id=f"session_{index:016}")
        old = "session_0000000000000000"
        await store.patch_session_display(owner=OWNER, session_id=old, patch={"pinned": True})
        with db.get_conn() as conn:
            conn.execute("UPDATE agent_kernel_sessions SET state_json='broken json' WHERE session_digest=?", (store._scope(OWNER, "session_0000000000000061")[1],))
            conn.execute("UPDATE agent_kernel_sessions SET state_json=json_set(state_json,'$.metadata.pinned',json('true')) WHERE session_digest=?", (store._scope(OWNER, "session_0000000000000060")[1],))
        items = await store.list_sessions(owner=OWNER)
        assert len(items) == 50
        assert items[0]["session_id"] == old and items[0]["pinned"] is True
        assert not any(item["session_id"].endswith(("0060", "0061")) for item in items)
        assert await store.list_sessions(owner="foreign") == []
    asyncio.run(exercise())


def _resources(*, ttl=900):
    items = [
        {"result_id": f"ux-resource-result-{position:03}", "title": f"Example.S01E0{position}.2160p",
         "site_id": "demo", "site_name": "Demo", "size_text": "2 GB", "download_state": "ready",
         "download_kinds": ["magnet"], "quality": {
             "tags": {"resolution": "2160p", "audio": "Atmos", "token": "SECRET"},
             "reasons": ["精确匹配", "https://10.0.0.9/token"], "warnings": ["需要人工确认", "/private/secret"],
         }, "rawresult": {"download_url": "https://10.0.0.9/private", "password": "RAWSECRET"},
         "torrent_url": "https://10.0.0.9/download?passkey=SECRET"}
        for position in (1, 2)
    ]
    result = ToolResult(True, "success", "找到候选", data={"items": items, "rawresult": "SECRET"})
    snapshot = safe_resource_snapshot(result, search_id=new_resource_search_id())
    result.references.append(ToolReference("resource_candidates", snapshot, ttl_seconds=ttl))
    return result


def _selection(view, positions=None, target="guangya"):
    return {"ref": view["selection_ref"], "positions": positions or [1], "target": target}


class SelectionModel:
    def __init__(self, *, forged_position=None):
        self.requests = []
        self.forged_position = forged_position

    async def stream(self, request, *, cancellation):
        cancellation.raise_if_cancelled()
        self.requests.append(request)
        intent = json.loads(request.messages[-1].content.splitlines()[-1])
        if self.forged_position is not None:
            intent["positions"] = [self.forged_position]
        yield ModelEvent(ModelEventType.TOOL_CALL_COMPLETED,
                         tool_call=ModelToolCall("selection-preview", "ingest.submit", intent))
        yield ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")


def _runtime(store=None, *, ttl=900, model=None):
    specs = {item.name: item for item in build_tool_specs(RecentResourceCandidateStore(), AgentIngestSessionStore())}
    specs["indexer.search_resources"] = replace(specs["indexer.search_resources"], handler=lambda _: _resources(ttl=ttl))
    catalog = catalog_from_tool_specs((specs["indexer.search_resources"], specs["indexer.present_candidates"], specs["ingest.submit"]))
    states = store or InMemorySessionStateStore()
    pipeline = ToolPipeline(catalog=catalog, state_store=states, reference_store=store)
    session = AgentSession(model=model or SelectionModel(), catalog=catalog, retriever=CapabilityRetriever(),
                           pipeline=pipeline, state_store=states)
    return session, pipeline, states


async def _publish_candidates(pipeline, states, *, owner=OWNER, session_id=SESSION, context=None):
    if context is None:
        lease, _ = await states.begin_turn(owner=owner, session_id=session_id, request_id="search")
        context = ToolCallContext(owner=owner, session_id=session_id, request_id="search", turn_id=lease.turn_id,
                                  lease=lease, cancellation=CancellationToken(), report_progress=AsyncMock())
    result = await pipeline.execute("indexer.search_resources", {"title": "Example"}, context=context)
    view = result.outcome.public_content["candidate_view"]
    if view is None:
        numbers = json.loads(result.outcome.model_content.partition("candidate_numbers=")[2].splitlines()[0])
        if not any(item["requested_episode"] for item in numbers):
            result = await pipeline.execute("indexer.present_candidates", {
                **result.outcome.public_content["reference_arguments"],
                "positions": [item["position"] for item in numbers],
            }, context=context)
            view = result.outcome.public_content["candidate_view"]
    return view, context


async def _events(stream):
    return [event async for event in stream]


def test_candidate_projection_is_allowlisted_and_current_view_restores(store):
    async def exercise():
        _, pipeline, states = _runtime(store)
        view, context = await _publish_candidates(pipeline, states)
        result = await pipeline.execute("indexer.search_resources", {"title": "Example"}, context=context)
        assert result.outcome.public_content["candidate_view"] is None
        presented = await pipeline.execute("indexer.present_candidates", {
            **result.outcome.public_content["reference_arguments"], "positions": [1, 2],
        }, context=context)
        view = presented.outcome.public_content["candidate_view"]
        public = json.dumps(presented.outcome.public_content, ensure_ascii=False)
        assert "result_id" not in public
        model = json.loads(result.outcome.model_content.partition("\nopaque_refs=")[0])
        assert model["data"]["items"][0]["result_id"] == "ux-resource-result-001"
        encoded = public + result.outcome.model_content
        for private in ("rawresult", "torrent_url", "SECRET", "10.0.0.9", "/private/", "https://"):
            assert private not in encoded
        assert set(view) == {"ref", "selection_ref", "expires_at", "items", "turn_id", "recommended_positions", "target", "target_source", "targets", "explicit_selection"}
        assert view["expires_at"] > time.time()
        item = view["items"][0]
        assert set(item) == {"position", "title", "site_name", "size_text", "tags", "reasons", "warnings", "coverage", "media_title", "media_scope", "requested_episode", "match"}
        # 展示只信引用内的安全快照，不从早先原始结果恢复额外字段。
        assert item["tags"] == {} and item["reasons"] == [] and item["warnings"] == []
        assert view["explicit_selection"] is True
        assert view["selection_ref"] != view["ref"]
        reloaded = SQLiteKernelStore(secret_provider=lambda: SECRET)
        state = await reloaded.load(owner=OWNER, session_id=SESSION)
        assert await current_candidate_view(state=state, store=reloaded) == view
        await reloaded.patch_session_display(owner=OWNER, session_id=SESSION, patch={"title": "重命名不丢候选", "pinned": True})
        state = await reloaded.load(owner=OWNER, session_id=SESSION)
        assert await current_candidate_view(state=state, store=reloaded) == view
        await states.begin_turn(owner=OWNER, session_id=SESSION, request_id="new-chat")
        latest = await reloaded.load(owner=OWNER, session_id=SESSION)
        assert await current_candidate_view(state=latest, store=reloaded) == view
    asyncio.run(exercise())


@pytest.mark.parametrize("attack", ["foreign_owner", "foreign_session", "expired", "old_snapshot", "position", "boolean", "unknown_ref", "snapshot_ref", "tampered_ref", "wall_expired"])
def test_invalid_selection_never_calls_model_provider_or_consumes_pending(store, attack):
    async def exercise():
        session, pipeline, states = _runtime(store)
        view, context = await _publish_candidates(pipeline, states)
        value = dict(_selection(view))
        owner, session_id = OWNER, SESSION
        if attack == "foreign_owner":
            owner = "foreign-owner"
            context_lease = await _seed(states, owner=owner)
        elif attack == "foreign_session":
            session_id = "foreign_session_123456"
            context_lease = await _seed(states, session_id=session_id)
        else:
            context_lease = context.lease
        if attack == "old_snapshot":
            await _publish_candidates(pipeline, states, context=context)
        if attack == "old_turn":
            context_lease, _ = await states.begin_turn(owner=owner, session_id=session_id, request_id="new-chat")
        if attack == "expired":
            # ReferenceStore 的 TTL 也是权威校验，不只信 UI 的 expires_at。
            store._clock = lambda: time.time() + 901
        if attack == "tampered_ref":
            with db.get_conn() as conn:
                conn.execute("UPDATE agent_kernel_refs SET value_hmac='tampered' WHERE ref_id=?", (value["ref"],))
        if attack == "wall_expired":
            value_view = dict(view, generation=context.lease.generation, expires_at=time.time() - 1)
            await states.commit(context_lease, updates=(StateUpdate(f"metadata.{CANDIDATE_VIEW_KEY}", value_view),))
        if attack == "position":
            value["positions"] = [13]  # 越界位置不能扩大快照。
        if attack == "boolean":
            value["positions"] = [True]
        if attack == "unknown_ref":
            value["ref"] = "ref_" + "z" * 24
        if attack == "snapshot_ref":
            value["ref"] = view["ref"]
        await states.commit(context_lease, updates=(StateUpdate("pending_effect_plan_id", "pending-must-survive"),))
        before = await states.load(owner=owner, session_id=session_id)
        rows_before = _row_snapshot()
        with patch.object(pipeline, "cancel_effect", new_callable=AsyncMock) as cancel, \
             patch.object(pipeline, "execute", new_callable=AsyncMock) as execute, \
             patch("app.agent.recent_resource_candidates.restore_resource_candidate_reference", side_effect=AssertionError("restore forbidden")) as restore:
            events = await _events(session.run(AgentInput(message="选择并预览", owner=owner, session_id=session_id, metadata={"selection": value})))
        assert len(events) == 1 and events[0].type is AgentEventType.TURN_FAILED
        assert events[0].payload["code"] == "selection_invalid"
        assert session.model.requests == []
        cancel.assert_not_called()
        execute.assert_not_called()
        restore.assert_not_called()
        assert await states.load(owner=owner, session_id=session_id) == before
        assert _row_snapshot() == rows_before
    asyncio.run(exercise())


def test_selection_guard_closes_cross_store_toctou_and_expiry(store):
    async def exercise():
        _, pipeline, states = _runtime(store)
        view, context = await _publish_candidates(pipeline, states)
        before = await states.load(owner=OWNER, session_id=SESSION)
        selection = await validate_selection(_selection(view), state=before, store=store)
        await _publish_candidates(pipeline, states, context=context)
        second_store = SQLiteKernelStore(secret_provider=lambda: SECRET)
        unchanged = await second_store.load(owner=OWNER, session_id=SESSION)
        with pytest.raises(SelectionInvalidError):
            await second_store.begin_turn(owner=OWNER, session_id=SESSION, request_id="stale-selection", selection_guard=selection.guard)
        assert await second_store.load(owner=OWNER, session_id=SESSION) == unchanged
        latest_selection = _selection(unchanged.metadata[CANDIDATE_VIEW_KEY])
        validated = await validate_selection(latest_selection, state=unchanged, store=store)
        with patch("app.agent.kernel.state.time.time", return_value=validated.guard.expires_at + 1), pytest.raises(SelectionInvalidError):
            await second_store.begin_turn(owner=OWNER, session_id=SESSION, request_id="expired-admission", selection_guard=validated.guard)
        assert await second_store.load(owner=OWNER, session_id=SESSION) == unchanged
    asyncio.run(exercise())


def test_selection_click_only_previews_and_explicit_confirmation_has_one_effect(store):
    async def exercise():
        from tests.test_agent_kernel_core import ScriptedModel
        model = ScriptedModel([[ModelEvent(ModelEventType.TEXT_DELTA, text="提交步骤已完成。"), ModelEvent(ModelEventType.FINISH, finish_reason="stop")]])
        session, pipeline, states = _runtime(store, model=model)
        view, _ = await _publish_candidates(pipeline, states)
        envelope = QueryEnvelope(owner=OWNER, session_id=SESSION, message="选择并预览", selection=_selection(view))
        with patch("app.agent.indexer_candidate_actions.prepare_submit_resource") as prepare, \
             patch("app.agent.indexer_candidate_actions.submit_resource_confirmed") as execute:
            prepare.side_effect = lambda args: (ToolResult(True, "confirmation_required", "确认后提交", data={"resource": {"title": "Example"}}), f"{args['result_id']}:{args['target']}")
            execute.return_value = ToolResult(True, "accepted", "已提交")
            events = await _events(session.run(envelope.to_agent_input()))
            approvals = [event for event in events if event.type is AgentEventType.EFFECT_APPROVAL_REQUIRED]
            assert len(approvals) == 1, [(event.type, event.payload) for event in events]
            prepare.assert_called_once()
            execute.assert_not_called()
            assert session.model.requests == []
            assert prepare.call_args.args[0]["target"] == "guangya"
            state = await states.load(owner=OWNER, session_id=SESSION)
            pending = state.pending_effect_plan_id
            assert pending == approvals[0].payload["plan"]["plan_id"]
            stale = await _events(session.run(envelope.to_agent_input()))
            assert stale[0].payload["code"] == "selection_invalid"
            assert (await states.load(owner=OWNER, session_id=SESSION)).pending_effect_plan_id == pending
            assert session.model.requests == []
            first = await _events(session.confirm(owner=OWNER, session_id=SESSION, plan_id=pending))
            assert any(event.type is AgentEventType.EFFECT_COMPLETED for event in first)
            completed = [event for event in first if event.type is AgentEventType.TURN_COMPLETED]
            assert completed
            assert "后台任务尚未完成" in completed[-1].payload["answer"]
            assert "提交步骤已完成" not in completed[-1].payload["answer"]
            await _events(session.confirm(owner=OWNER, session_id=SESSION, plan_id=pending))
            execute.assert_called_once()
            assert len(session.model.requests) == 1  # 已受理回执继续交给 Agent 说明状态，但不得再次执行。
    asyncio.run(exercise())


def test_pipeline_rejects_model_rewriting_verified_choice_before_provider(store):
    async def exercise():
        _, pipeline, states = _runtime(store)
        view, context = await _publish_candidates(pipeline, states)
        state = await states.load(owner=OWNER, session_id=SESSION)
        verified = await validate_selection(_selection(view), state=state, store=store)
        context = replace(context, selection_arguments=verified.arguments)
        with patch("app.agent.indexer_candidate_actions.prepare_submit_resource") as prepare:
            for arguments in ({**verified.arguments, "positions": [2]},
                              {**verified.arguments, "positions": [True]},
                              {**verified.arguments, "target": "both"}):
                with pytest.raises(ToolPipelineError, match="只能预览"):
                    await pipeline.execute("ingest.submit", arguments, context=context)
            with pytest.raises(ToolPipelineError, match="只能预览"):
                await pipeline.execute("indexer.search_resources", {"title": "重新搜索"}, context=context)
            prepare.assert_not_called()
    asyncio.run(exercise())


def test_get_session_restores_only_valid_current_candidates_and_query_accepts_selection(api):
    _login(api)
    owner = _web_owner(api)
    _, pipeline, states = _runtime(api.store)
    view, _ = asyncio.run(_publish_candidates(pipeline, states, owner=owner))
    response = api.client.get(f"/api/agent/sessions/{SESSION}")
    assert response.status_code == 200 and response.json()["candidate_view"] == view
    class Web:
        async def query(self, envelope, *, cancellation=None):
            assert envelope.to_agent_input().metadata["selection"] == _selection(view)
            yield b'{"type":"turn.completed"}\n'
    api.runtime.web = Web()
    response = api.client.post("/api/agent/query", json={
        "message": "选择并预览", "session_id": SESSION, "selection": _selection(view), "stream": True,
    }, headers=CSRF)
    assert response.status_code == 200 and "turn.completed" in response.text
    for selection in (None, {}, {"ref": view["ref"], "position": True}, {**_selection(view), "url": "https://private"}):
        assert api.client.post("/api/agent/query", json={"message": "选择", "session_id": SESSION, "selection": selection}, headers=CSRF).status_code == 400
    api.principal["username"] = "foreign"
    assert api.client.get(f"/api/agent/sessions/{SESSION}").json()["candidate_view"] is None


def test_patch_preserves_signed_unknown_fields_and_full_conversation_json(store):
    asyncio.run(_seed(store))
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM agent_kernel_sessions").fetchone()
        payload = json.loads(row["state_json"])
        payload["future_field"] = {"inflight_receipt": ["do-not-rewrite", 7]}
        payload["conversation"] = [{"role": "user", "content": str(index)} for index in range(85)]
        domain = f"state:v1:{row['owner_digest']}:{row['session_digest']}:{row['generation']}".encode()
        encoded, signature = store._encode(payload, domain=domain, maximum=store.max_state_bytes)
        conn.execute("UPDATE agent_kernel_sessions SET state_json=?,state_hmac=?", (encoded, signature))
    asyncio.run(store.patch_session_display(owner=OWNER, session_id=SESSION, patch={"pinned": True}))
    with db.get_conn() as conn:
        after = conn.execute("SELECT * FROM agent_kernel_sessions").fetchone()
    updated = store._decode(after["state_json"], after["state_hmac"], domain=domain, expected_type=dict)
    assert updated == {**payload, "metadata": {**payload["metadata"], "pinned": True}}
    assert after["generation"] == row["generation"] and after["updated_at"] == row["updated_at"]


@pytest.mark.parametrize("failed", [False, True])
def test_empty_or_failed_same_turn_search_invalidates_old_candidate(store, failed):
    async def exercise():
        session, pipeline, states = _runtime(store)
        view, context = await _publish_candidates(pipeline, states)
        original = pipeline.catalog.get("indexer.search_resources")
        read = Mock(side_effect=RuntimeError("offline")) if failed else Mock(return_value=ToolResult(
            True, "empty", "没有候选", data={"items": [], "rawresult": {"url": "https://10.0.0.9/private"}},
        ))
        catalog = type(pipeline.catalog)([replace(original, read=read), pipeline.catalog.get("ingest.submit")])
        newer = ToolPipeline(catalog=catalog, state_store=states, reference_store=store)
        if failed:
            with pytest.raises(ToolPipelineError, match="工具执行失败"):
                await newer.execute("indexer.search_resources", {"title": "new search"}, context=context)
        else:
            result = await newer.execute("indexer.search_resources", {"title": "new search"}, context=context)
            assert result.outcome.public_content["candidate_view"] is None
            assert "rawresult" not in str(result.outcome.public_content) + result.outcome.model_content
        state = await states.load(owner=OWNER, session_id=SESSION)
        assert await current_candidate_view(state=state, store=store) is None
        events = await _events(session.run(AgentInput(message="选择并预览", owner=OWNER, session_id=SESSION,
                                                     metadata={"selection": _selection(view)})))
        assert events[0].payload["code"] == "selection_invalid"
        assert session.model.requests == []
    asyncio.run(exercise())


def test_scope_binds_to_owner_used_for_session_list_even_during_account_change(api):
    _login(api)
    expected = api.client.get("/api/agent/sessions").json()["draft_scope"]
    original = api.store.list_sessions
    async def list_then_rotate(*, owner):
        result = await original(owner=owner)
        api.principal["username"] = "rotated-principal"
        return result
    with patch.object(api.store, "list_sessions", side_effect=list_then_rotate):
        response = api.client.get("/api/agent/sessions")
    assert response.json()["draft_scope"] == expected


def test_concurrent_selection_admission_accepts_only_one_cross_store_turn(store):
    async def exercise():
        _, pipeline, states = _runtime(store)
        view, _ = await _publish_candidates(pipeline, states)
        state = await states.load(owner=OWNER, session_id=SESSION)
        selection = await validate_selection(_selection(view), state=state, store=store)
        other = SQLiteKernelStore(secret_provider=lambda: SECRET)
        outcomes = await asyncio.gather(*(
            local.begin_turn(owner=OWNER, session_id=SESSION, request_id=f"click-{index}", selection_guard=selection.guard)
            for index, local in enumerate((store, other))
        ), return_exceptions=True)
        assert sum(isinstance(outcome, SelectionInvalidError) for outcome in outcomes) == 1
        accepted = next(outcome for outcome in outcomes if isinstance(outcome, tuple))
        assert accepted[0].generation == state.generation + 1
        assert (await store.load(owner=OWNER, session_id=SESSION)).generation == state.generation + 1
    asyncio.run(exercise())


@pytest.mark.parametrize("second_result", ["failed", "empty"])
def test_same_turn_read_invalidation_reaches_real_progress_stream_before_terminal_event(store, second_result):
    class SearchTwiceModel:
        def __init__(self):
            self.requests = []

        async def stream(self, request, *, cancellation):
            cancellation.raise_if_cancelled()
            self.requests.append(request)
            if len(self.requests) <= 2:
                yield ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall(
                    f"search-{len(self.requests)}", "indexer.search_resources", {"title": "Example"},
                ))
                yield ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")
            else:
                yield ModelEvent(ModelEventType.TEXT_DELTA, text="第二次搜索没有可选候选。")
                yield ModelEvent(ModelEventType.FINISH, finish_reason="stop")

    async def exercise():
        _, original, states = _runtime(store)
        reader = Mock(side_effect=[
            _episode_candidates_result(match="exact_episode"), RuntimeError("offline") if second_result == "failed" else ToolResult(True, "empty", "无候选", data={"items": []}),
        ])
        catalog = type(original.catalog)([replace(original.catalog.get("indexer.search_resources"), read=reader)])
        pipeline = ToolPipeline(catalog=catalog, state_store=states, reference_store=store)
        model = SearchTwiceModel()
        session = AgentSession(model=model, catalog=catalog, retriever=CapabilityRetriever(), pipeline=pipeline, state_store=states)
        events = await _events(session.run(AgentInput(message="搜索资源，再重新搜索", owner=OWNER, session_id=SESSION)))
        completed = [event for event in events if event.type is AgentEventType.TOOL_COMPLETED]
        first_view = completed[0].payload["result"]["candidate_view"]
        invalidations = [event for event in events if event.type is AgentEventType.TOOL_PROGRESS and "candidate_view" in event.payload]
        assert len(invalidations) == 2 and all(event.payload["candidate_view"] is None for event in invalidations)
        assert invalidations[1].payload["tool"] == "indexer.search_resources"
        terminal = next(event for event in events if event.type is AgentEventType.TOOL_FAILED) if second_result == "failed" else completed[1]
        assert completed[0].sequence < invalidations[1].sequence < terminal.sequence
        if second_result == "empty":
            assert terminal.payload["result"]["candidate_view"] is None
        state = await states.load(owner=OWNER, session_id=SESSION)
        assert await current_candidate_view(state=state, store=store) is None
        assert len(model.requests) == 3
        rejected = await _events(session.run(AgentInput(message="选择并预览", owner=OWNER, session_id=SESSION,
                                                       metadata={"selection": _selection(first_view)})))
        assert rejected[0].payload["code"] == "selection_invalid" and len(model.requests) == 3
    asyncio.run(exercise())


@pytest.mark.parametrize("rejection", ["arguments", "rate_limit"])
def test_pre_read_rejection_keeps_current_card_and_emits_no_invalidation(store, rejection):
    async def exercise():
        _, pipeline, states = _runtime(store)
        view, context = await _publish_candidates(pipeline, states)
        context.report_progress.reset_mock()
        await states.commit(context.lease, updates=(StateUpdate("pending_effect_plan_id", "keep-pending"),))
        before = await states.load(owner=OWNER, session_id=SESSION)
        if rejection == "rate_limit":
            pipeline.rate_limiter.acquire = AsyncMock(side_effect=ToolPipelineError("limited", code="rate_limited"))
        with pytest.raises(ToolPipelineError):
            await pipeline.execute("indexer.search_resources", {} if rejection == "arguments" else {"title": "new search"}, context=context)
        context.report_progress.assert_not_called()
        after = await states.load(owner=OWNER, session_id=SESSION)
        assert after == before
        assert await current_candidate_view(state=after, store=store) == view
        assert await validate_selection(_selection(view), state=after, store=store)
    asyncio.run(exercise())


def test_keys_only_listing_continues_past_fifty_bad_signatures_without_schema_change(store):
    async def exercise():
        for index in range(115):
            await _seed(store, session_id=f"key_session_{index:016}")
        oldest = "key_session_0000000000000000"
        await store.patch_session_display(owner=OWNER, session_id=oldest, patch={"pinned": True})
        with db.get_conn() as conn:
            schema = [tuple(row) for row in conn.execute("SELECT name,sql FROM sqlite_master ORDER BY name")]
            for index in range(55, 115):
                conn.execute(
                    "UPDATE agent_kernel_sessions SET state_json=json_set(state_json,'$.metadata.pinned',json('true')),state_hmac='invalid' "
                    "WHERE session_digest=?", (store._scope(OWNER, f"key_session_{index:016}")[1],),
                )
            conn.execute("UPDATE agent_kernel_sessions SET state_json='bad json' WHERE session_digest=?", (store._scope(OWNER, "key_session_0000000000000054")[1],))
        result = await store.list_sessions(owner=OWNER)
        assert len(result) == 50 and result[0]["session_id"] == oldest
        assert all(int(item["session_id"].rsplit("_", 1)[-1]) < 54 for item in result)
        with db.get_conn() as conn:
            assert [tuple(row) for row in conn.execute("SELECT name,sql FROM sqlite_master ORDER BY name")] == schema
    asyncio.run(exercise())


def test_keys_only_listing_uses_one_read_snapshot_during_cross_connection_generation_and_pin_update(store):
    import threading

    async def seed():
        for index in range(3):
            await _seed(store, session_id=f"snapshot_{index:016}")
    asyncio.run(seed())
    target = "snapshot_0000000000000000"
    before = store._list_sessions_sync(OWNER, 50)
    other = SQLiteKernelStore(secret_provider=lambda: SECRET)
    trigger, finished = threading.Event(), threading.Event()
    errors = []

    def writer():
        try:
            assert trigger.wait(10)
            lease, _ = other._begin_turn_sync(OWNER, target, "concurrent-new-turn")
            other._commit_sync(lease, [{"role": "user", "content": "并发新回合"}], ())
            other._patch_session_display_sync(OWNER, target, {"title": "并发标题", "pinned": True})
        except Exception as exc:  # noqa: BLE001 - 把后台线程失败交给主线程断言
            errors.append(exc)
        finally:
            finished.set()

    worker = threading.Thread(target=writer)
    worker.start()
    decode = store._decode
    first = True

    def decode_during_write(*args, **kwargs):
        nonlocal first
        if first:
            first = False
            trigger.set()
            assert finished.wait(10), "WAL 写入不应被列表读快照阻塞"
            assert not errors, errors
        return decode(*args, **kwargs)

    try:
        with patch.object(store, "_decode", side_effect=decode_during_write):
            observed = store._list_sessions_sync(OWNER, 50)
    finally:
        trigger.set()
        worker.join(10)
    assert not worker.is_alive() and not errors
    assert observed == before, "按键回读不能混入已经提交的新 generation/HMAC/显示元数据"
    latest = store._list_sessions_sync(OWNER, 50)
    assert latest[0]["session_id"] == target and latest[0]["pinned"] is True
    assert latest[0]["title"] == "并发标题" and latest[0]["generation"] == 2


def test_listing_sql_sorts_only_small_keys_and_explicitly_opens_read_transaction(store):
    from contextlib import contextmanager

    asyncio.run(_seed(store))
    original = db.get_conn
    statements = []

    @contextmanager
    def traced_conn():
        with original() as conn:
            conn.set_trace_callback(statements.append)
            yield conn

    with patch.object(db, "get_conn", traced_conn):
        assert len(store._list_sessions_sync(OWNER, 50)) == 1
    ordered = next(sql for sql in statements if "ORDER BY CASE WHEN json_valid" in sql)
    assert ordered.partition("FROM")[0].strip() == "SELECT session_digest"
    assert any(sql.strip() == "BEGIN" for sql in statements)
    assert "LIMIT" not in ordered, "不能在 HMAC 核验之前截断有效名额"


def test_verified_cross_series_recommendations_keep_identity_and_global_positions():
    from app.agent.kernel.ux_selection import _recommend, candidate_item

    names = ["光阴之外", "择日飞升", "大主宰", "牧神记", "沧元图", "一斩苍穹"]
    items = [candidate_item({
        "title": f"{name}.S01E08.2160p", "match": "exact_episode", "_verification_context": {"title": name, "season": 1, "episode": 8},
    }, position) for position, name in enumerate(names, 1)]
    assert _recommend(items) == [1, 2, 3, 4, 5, 6]
    assert [item["media_title"] for item in items] == names
    assert all(item["requested_episode"] == [1, 8] for item in items)
    assert all(candidate_item(item, item["position"]) == item for item in items)
    alternatives = [*items, candidate_item({
        "title": "光阴之外.S01E08.1080p", "match": "exact_episode", "_verification_context": {"title": names[0], "season": 1, "episode": 8},
    }, 7)]
    assert _recommend(alternatives) == [1, 2, 3, 4, 5, 6]


def test_same_title_different_tmdb_id_are_distinct_recommendation_scopes():
    from app.agent.kernel.ux_selection import _recommend, candidate_item
    items = [candidate_item({"title": "同名剧.S01E01", "match": "exact_episode", "_verification_context": {
        "title": "同名剧", "tmdb_id": str(identity), "season": 1, "episode": 1,
    }}, position) for position, identity in enumerate((111, 222), 1)]
    assert _recommend(items) == [1, 2]
    assert [item["media_scope"] for item in items] == ["tmdb:111", "tmdb:222"]


def test_general_search_does_not_recommend_old_episodes_or_suggest_ingest(store):
    async def exercise():
        _, pipeline, states = _runtime(store)
        view, context = await _publish_candidates(pipeline, states)
        result = await pipeline.execute("indexer.search_resources", {"title": "Example"}, context=context)
        assert view["items"], "普通搜索仍应允许用户手动挑选"
        assert view["recommended_positions"] == []
        assert "recommended_ingest_arguments=" not in result.outcome.model_content
        assert "candidate_numbers=" in result.outcome.model_content
        view, _ = await _publish_candidates(pipeline, states, context=context)
        # 旧会话曾按标题给出自动推荐，重新读取时不能继续沿用。
        state = await states.load(owner=OWNER, session_id=SESSION)
        state.metadata[CANDIDATE_VIEW_KEY]["recommended_positions"] = [1]
        restored = await current_candidate_view(state=state, store=store)
        assert restored["recommended_positions"] == []
        selection = await validate_selection(_selection(restored), state=state, store=store)
        assert selection.arguments["positions"] == [1]
    asyncio.run(exercise())


@pytest.mark.parametrize("match", ["", "unknown", "season_pack", "conflict"])
def test_missing_episode_context_alone_does_not_prove_candidate_coverage(match):
    from app.agent.kernel.ux_selection import _recommend, candidate_item

    item = candidate_item({
        "title": "[GM-Team][东大高武学院][01-04][4K]", "match": match,
        "_verification_context": {"title": "东大高武学院", "season": 1, "episode": 9},
    }, 1)
    assert _recommend([item]) == []
    assert _recommend([]) == []


def test_verified_match_recommendations_use_domain_evidence_not_generic_title_ranges():
    from app.agent.kernel.ux_selection import _recommend, candidate_item

    candidates = [
        {"title": "东大高武学院 S01E01-E04", "match": "conflict"},
        {"title": "东大高武学院 S01E08", "match": "conflict"},
        {"title": "东大高武学院 全集", "match": "season_pack"},
        {"title": "东大高武学院 S01E08-E09", "match": "episode_pack"},
        {"title": "东大高武学院 S01E09", "match": "exact_episode"},
    ]
    items = [candidate_item({**item, "_verification_context": {
        "title": "东大高武学院", "season": 1, "episode": 9,
    }}, position) for position, item in enumerate(candidates, 1)]
    assert _recommend(items) == [4]
    assert all(candidate_item(item, item["position"]) == item for item in items)
    # 普通搜索不携带缺集核验，仅标题/匹配标签不能成为补缺集证据。
    assert _recommend([candidate_item(item, position) for position, item in enumerate(candidates, 1)]) == []


def _episode_candidates_result(match="unknown", *, include_match=False):
    from tests.test_agent_kernel_resource_ingest import _multi_work_candidate

    candidates = [{**_multi_work_candidate(1, "仙逆", "111"), "match": match}]
    if include_match:
        candidates.append(_multi_work_candidate(2, "完美世界", "222"))
    snapshot = {"search_id": new_resource_search_id(), "search_status": "success", "candidates": candidates}
    result = ToolResult(True, "success", "已检索，缺集覆盖仍需核对", data={"items": candidates})
    result.references.append(ToolReference("resource_candidates", snapshot))
    return result


@pytest.mark.parametrize("match", ["unknown", "season_pack"])
def test_missing_episode_search_without_verified_coverage_does_not_issue_a_card(store, match):
    async def exercise():
        _, pipeline, states = _runtime(store)
        _, context = await _publish_candidates(pipeline, states)
        with patch(__name__ + "._resources", return_value=_episode_candidates_result(match)):
            result = await pipeline.execute("indexer.search_resources", {"title": "仙逆"}, context=context)
        assert result.outcome.public_content["candidate_view"] is None
        assert result.outcome.public_content["summary"] == "已检索,缺集覆盖仍需核对"
        assert "candidate_numbers=" not in result.outcome.model_content
        state = await states.load(owner=OWNER, session_id=SESSION)
        assert state.metadata[CANDIDATE_VIEW_KEY] is None
        assert await current_candidate_view(state=state, store=store) is None
    asyncio.run(exercise())


@pytest.mark.parametrize("legacy_empty", [False, True])
def test_restored_empty_or_unverified_episode_card_is_not_resurrected(store, legacy_empty):
    from app.agent.kernel.ux_selection import candidate_item

    async def exercise():
        _, pipeline, states = _runtime(store)
        view, _ = await _publish_candidates(pipeline, states)
        state = await states.load(owner=OWNER, session_id=SESSION)
        snapshot = _episode_candidates_result().references[0].value
        ref = await store.put(owner=OWNER, session_id=SESSION, kind="resource_candidates", value=snapshot)
        selection = await store.put(owner=OWNER, session_id=SESSION, kind="ux_resource_selection", value={
            "ref": ref.ref, "generation": state.generation, "expires_at": view["expires_at"],
        })
        state.metadata[CANDIDATE_VIEW_KEY] = {
            **view, "generation": state.generation, "ref": ref.ref, "selection_ref": selection.ref,
            "items": [] if legacy_empty else [candidate_item(item, item["position"]) for item in snapshot["candidates"]],
            "recommended_positions": [1],
        }
        assert await current_candidate_view(state=state, store=store) is None
    asyncio.run(exercise())


def test_mixed_episode_candidates_keep_verified_global_positions(store):
    async def exercise():
        _, pipeline, states = _runtime(store)
        with patch(__name__ + "._resources", return_value=_episode_candidates_result(include_match=True)):
            view, _ = await _publish_candidates(pipeline, states)
        assert view["recommended_positions"] == [2]
        assert [item["position"] for item in view["items"]] == [1, 2]
        state = await states.load(owner=OWNER, session_id=SESSION)
        assert (await current_candidate_view(state=state, store=store))["recommended_positions"] == [2]
        result = await validate_selection(_selection(view, [2]), state=state, store=store)
        assert result.arguments["positions"] == [2]
    asyncio.run(exercise())


def test_raw_search_keeps_model_evidence_without_publishing_unselected_cards(store):
    async def exercise():
        _, pipeline, states = _runtime(store)
        lease, _ = await states.begin_turn(owner=OWNER, session_id=SESSION, request_id="remaining-resources")
        context = ToolCallContext(owner=OWNER, session_id=SESSION, request_id="remaining-resources", turn_id=lease.turn_id,
                                  lease=lease, cancellation=CancellationToken(), report_progress=AsyncMock())
        for title in ("狐妖小红娘", "Fox Spirit Matchmaker"):
            result = await pipeline.execute("indexer.search_resources", {"title": title}, context=context)
            assert result.outcome.public_content["candidate_view"] is None
            assert "candidate_numbers=" in result.outcome.model_content
            assert "resource_candidates_ref" in result.outcome.model_content
            assert "recommended_ingest_arguments=" not in result.outcome.model_content
        state = await states.load(owner=OWNER, session_id=SESSION)
        assert state.metadata[CANDIDATE_VIEW_KEY] is None
        assert await current_candidate_view(state=state, store=store) is None
    asyncio.run(exercise())


def test_legacy_generic_card_does_not_reappear_as_a_recommendation(store):
    async def exercise():
        _, pipeline, states = _runtime(store)
        view, _ = await _publish_candidates(pipeline, states)
        state = await states.load(owner=OWNER, session_id=SESSION)
        state.metadata[CANDIDATE_VIEW_KEY].pop("explicit_selection", None)
        state.metadata[CANDIDATE_VIEW_KEY]["recommended_positions"] = [1]
        assert view["items"]
        assert await current_candidate_view(state=state, store=store) is None
    asyncio.run(exercise())


def test_explicit_presentation_preserves_positions_and_can_clear_the_view(store):
    async def exercise():
        _, pipeline, states = _runtime(store)
        original, context = await _publish_candidates(pipeline, states)
        result = await pipeline.execute("indexer.present_candidates", {
            "resource_candidates_ref": original["ref"], "positions": [2],
        }, context=context)
        view = result.outcome.public_content["candidate_view"]
        assert [item["position"] for item in view["items"]] == [2]
        assert view["explicit_selection"] is True
        assert view["recommended_positions"] == []
        state = await states.load(owner=OWNER, session_id=SESSION)
        assert (await current_candidate_view(state=state, store=store))["items"] == view["items"]
        assert (await validate_selection(_selection(view, [2]), state=state, store=store)).arguments["positions"] == [2]
        with pytest.raises(SelectionInvalidError):
            await validate_selection(_selection(view, [1]), state=state, store=store)
        cleared = await pipeline.execute("indexer.present_candidates", {
            "resource_candidates_ref": view["ref"], "positions": [],
        }, context=context)
        assert cleared.outcome.public_content["candidate_view"] is None
        state = await states.load(owner=OWNER, session_id=SESSION)
        assert await current_candidate_view(state=state, store=store) is None
    asyncio.run(exercise())


def test_explicit_presentation_cannot_override_missing_episode_coverage(store):
    async def exercise():
        _, pipeline, states = _runtime(store)
        _, context = await _publish_candidates(pipeline, states)
        with patch(__name__ + "._resources", return_value=_episode_candidates_result("unknown")):
            searched = await pipeline.execute("indexer.search_resources", {"title": "缺集"}, context=context)
        presented = await pipeline.execute("indexer.present_candidates", {
            **searched.outcome.public_content["reference_arguments"], "positions": [1],
        }, context=context)
        assert presented.outcome.public_content["candidate_view"] is None
        assert "recommended_ingest_arguments=" not in presented.outcome.model_content
    asyncio.run(exercise())


@pytest.mark.parametrize("scope", ["expired", "foreign_owner", "foreign_session"])
def test_present_requires_a_live_reference_in_this_session(store, scope):
    async def exercise():
        _, pipeline, states = _runtime(store)
        view, context = await _publish_candidates(pipeline, states)
        if scope == "expired":
            store._clock = lambda: time.time() + 901
        else:
            owner, session_id = ("another-owner", SESSION) if scope == "foreign_owner" else (OWNER, "another_session_123456")
            lease, _ = await states.begin_turn(owner=owner, session_id=session_id, request_id="other")
            context = replace(context, owner=owner, session_id=session_id, lease=lease, turn_id=lease.turn_id)
        with pytest.raises(ToolPipelineError) as error:
            await pipeline.execute("indexer.present_candidates", {
                "resource_candidates_ref": view["ref"], "positions": [1],
            }, context=context)
        assert error.value.code == "reference_invalid"
    asyncio.run(exercise())


def test_cloud_remaining_resource_search_has_no_cards_without_relevant_selection(store):
    from app.agent.kernel.adapters import TurnViewBuilder
    from app.agent.public_view import public_conversation_messages

    class ResearchModel:
        def __init__(self):
            self.requests = []

        async def stream(self, request, *, cancellation):
            self.requests.append(request)
            if len(self.requests) <= 2:
                yield ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall(
                    f"search-{len(self.requests)}", "indexer.search_resources",
                    {"title": "狐妖小红娘" if len(self.requests) == 1 else "Fox Spirit Matchmaker"},
                ))
                yield ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")
            else:
                yield ModelEvent(ModelEventType.TEXT_DELTA, text="本次未找到 S01E168～S01E183 的匹配资源。已有命中仅包含旧集，不作为补缺候选。")
                yield ModelEvent(ModelEventType.FINISH, finish_reason="stop")

    async def exercise():
        model = ResearchModel()
        session, _, states = _runtime(store, model=model)
        lease, _ = await states.begin_turn(owner=OWNER, session_id=SESSION, request_id="cloud-observation")
        await states.commit(lease, conversation=[
            {"role": "user", "content": "云盘根目录/狐妖小红娘缺少哪些集数？"},
            {"role": "assistant", "content": "目录观察已有S01E01～167；需查找的范围是S01E168～183。"},
        ])
        template = _resources().data["items"][0]
        old_episodes = [{**template, "result_id": f"fox-old-result-{episode:04d}",
                         "title": f"狐妖小红娘.S01E{episode:03d}.2160p"} for episode in range(145, 157)]
        searched = ToolResult(True, "success", "找到12项同名旧资源", data={"items": old_episodes})
        searched.references.append(ToolReference("resource_candidates", safe_resource_snapshot(
            searched, search_id=new_resource_search_id(),
        )))
        with patch(__name__ + "._resources", return_value=searched):
            events = await _events(session.run(AgentInput(
                message="帮我查找剩余资源",
                owner=OWNER, session_id=SESSION,
            )))
        accumulator = TurnViewBuilder()
        for event in events:
            accumulator.apply(event)
            if event.type is AgentEventType.TOOL_COMPLETED:
                assert event.payload["result"]["candidate_view"] is None
        view = accumulator.build()
        assert view.status == "success"
        assert "未找到" in view.answer and "S01E168" in view.answer
        assert view.candidate_view is None
        assert len(model.requests) == 3
        state = await states.load(owner=OWNER, session_id=SESSION)
        restored = await current_candidate_view(state=state, store=store)
        assert restored is None
        messages = public_conversation_messages(state.conversation, candidate_view=restored)
        assert not any(message.get("candidate_view") for message in messages)
        assert any("未找到" in message["content"] for message in messages)
    asyncio.run(exercise())


def test_general_resource_search_can_explicitly_present_a_relevant_subset(store):
    from app.agent.kernel.adapters import TurnViewBuilder
    from app.agent.public_view import public_conversation_messages

    class PickModel:
        def __init__(self):
            self.calls = 0

        async def stream(self, request, *, cancellation):
            self.calls += 1
            if self.calls == 1:
                tool = ModelToolCall("search", "indexer.search_resources", {"title": "Example"})
            elif self.calls == 2:
                evidence = next(message.content for message in reversed(request.messages) if message.role == "tool")
                refs = json.loads(evidence.partition("reference_arguments=")[2].splitlines()[0])
                tool = ModelToolCall("present", "indexer.present_candidates", {**refs, "positions": [2]})
            else:
                yield ModelEvent(ModelEventType.TEXT_DELTA, text="已挑选第2个版本，可先预览，尚未下载。")
                yield ModelEvent(ModelEventType.FINISH, finish_reason="stop")
                return
            yield ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=tool)
            yield ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")

    async def exercise():
        session, _, states = _runtime(store, model=PickModel())
        events = await _events(session.run(AgentInput(message="找Example资源，挑选合适的版本", owner=OWNER, session_id=SESSION)))
        builder = TurnViewBuilder()
        for event in events:
            builder.apply(event)
        result = builder.build()
        assert result.status == "success"
        assert result.approval is None
        assert result.candidate_view["explicit_selection"] is True
        assert [item["position"] for item in result.candidate_view["items"]] == [2]
        state = await states.load(owner=OWNER, session_id=SESSION)
        restored = await current_candidate_view(state=state, store=store)
        messages = public_conversation_messages(state.conversation, candidate_view=restored)
        assert sum(bool(message.get("candidate_view")) for message in messages) == 1
        assert "尚未下载" in result.answer
    asyncio.run(exercise())
