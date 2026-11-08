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
from tests.resource_presentation_test_harness import (
    new_resource_search_id,
    safe_resource_snapshot,
)
from app.agent.domains.resources.selection import (
    CANDIDATE_VIEW_KEY,
    current_candidate_view,
    validate_selection,
)
from app.agent.errors import ToolPipelineError

from app.agent.kernel.events import AgentEventType
from app.agent.kernel.model import ModelEvent, ModelEventType, ModelToolCall
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.session import AgentSession
from app.agent.kernel.state import InMemorySessionStateStore
from app.agent.kernel.state_types import (
    AgentInput,
    CancellationToken,
    SelectionInvalidError,
    StateUpdate,
)
from app.agent.kernel.tool_calls import ToolCallContext
from app.agent.models import ToolReference, ToolResult
from app.agent.transports import QueryEnvelope
from app.routes import agent_api
from tests.resource_presentation_test_harness import present, observe_fixture, model_resource_candidates

SECRET = "agent-ux-backend-test-secret"
OWNER = "owner-ux"
SESSION = "session_ux_12345678"
CSRF = {"X-CSRF-Token": "ux-csrf"}


@pytest.fixture
def store(tmp_path):
    from tests.support import initialized_web_runtime, isolated_indexer_results

    # Store and API middleware share one explicitly installed physical database.
    # Do not import main against a schema-only DB or bypass startup admission.
    with initialized_web_runtime(), isolated_indexer_results():
        yield SQLiteKernelStore(secret_provider=lambda: SECRET)


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

    session, _, _ = _runtime(store)
    runtime = SimpleNamespace(store=store, session=session,
                              web=SimpleNamespace(activity=AsyncMock(return_value=None)))
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
    reader = Mock(return_value={"ok": True, "status": "empty", "data": {"snapshot_status": "empty"}})
    monkeypatch.setattr(agent_api, "read_next_actions", reader)
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
    reader.assert_called_once_with()
    runtime.assert_not_called()


def test_next_actions_existing_local_projection_max_three_and_unavailable(api, monkeypatch):
    _login(api)
    sources = [("downloads", "download_needs_review"), ("rss", "rss_failed"),
               ("organize", "organize_issue"), ("strm", "strm_open_failure")]
    todo = {"ok": True, "status": "attention", "data": {
        "areas": [{"source": source, "status": "attention", "attention_count": 1,
                   "reason_codes": [reason]} for source, reason in sources],
    }}
    with patch("app.modules.workspace._backlog", return_value=todo) as reader:
        response = api.client.get("/api/agent/next-actions")
    assert response.status_code == 200
    payload = response.json()
    assert payload["snapshot_status"] == "attention"
    assert len(payload["actions"]) == 3
    assert [item["id"] for item in payload["actions"]] == ["review_downloads", "review_rss", "review_organize"]
    assert all(set(item) == {"id", "title", "description", "prompt"} for item in payload["actions"])
    reader.assert_called_once_with()
    monkeypatch.setattr(agent_api, "read_next_actions", Mock(side_effect=RuntimeError("https://private/token")))
    failed = api.client.get("/api/agent/next-actions")
    assert failed.status_code == 200
    assert failed.json() == {"actions": [], "snapshot_status": "unavailable"}
    monkeypatch.setattr(agent_api, "read_next_actions", lambda: {"ok": False, "status": "unavailable"})
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


@pytest.mark.parametrize("binding", ["", "old-binding", "f" * 64])
def test_retired_tg_header_cannot_read_or_modify_telegram_session(api, binding):
    _login(api)
    tg_owner = "tg:v1:100\x1f200"
    asyncio.run(_seed(api.store, owner=tg_owner))
    tg_before = asyncio.run(api.store.load(owner=tg_owner, session_id=SESSION))
    headers = {**CSRF, "X-Agent-Handoff": binding}
    path = f"/api/agent/sessions/{SESSION}"
    response = api.client.get(path, headers=headers)
    assert response.status_code == 200
    assert response.json()["messages"] == []
    assert response.json()["pending_approval"] is None
    assert api.client.patch(path, headers=headers, json={"title": "不得改TG"}).status_code == 404
    asyncio.run(_seed(api.store, owner=_web_owner(api)))
    assert api.client.patch(path, headers=headers, json={"title": "仅Web"}).status_code == 200
    tg_after = asyncio.run(api.store.load(owner=tg_owner, session_id=SESSION))
    assert tg_after == tg_before
    sessions = api.client.get("/api/agent/sessions", headers=headers).json()["sessions"]
    assert len(sessions) == 1 and sessions[0]["title"] == "仅Web"


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
            second_store.commit(lease, conversation=[{"role": "user", "content": "新回合内容", "history_id": "concurrent-user-event"}],
                                updates=(StateUpdate("metadata.new_turn", True),)),
            store.patch_session_display(owner=OWNER, session_id=SESSION, patch={"pinned": True}),
        )
        state = await second_store.load(owner=OWNER, session_id=SESSION)
        assert state.conversation == [{"role": "user", "content": "新回合内容", "history_id": "concurrent-user-event", "history_order": 1}]
        assert state.metadata == {
            "inflight": {"operation": "protected"}, "title": "用户标题", "pinned": True,
            "new_turn": True,
            "active_publication": {"generation": lease.generation, "turn_id": lease.turn_id,
                                   "request_id": "seed", "status": "running"},
        }
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


def _cache_candidates(items):
    from app.indexers.runtime import get_indexer_service
    from app.indexers.models import IndexerItem
    for item in items:
        get_indexer_service().result_store.restore(item["result_id"], IndexerItem(
            title=item["title"], site_id=item.get("site_id", "demo"), site_name=item.get("site_name", "Demo"),
            magnet="magnet:?xt=urn:btih:" + "a" * 40, download_state="ready", download_kinds=("magnet",),
        ))


def _resources(*, ttl=900):
    from app.indexers.models import IndexerItem

    size = IndexerItem("demo", "Demo", "Example", size_bytes=377_162_579).to_public_dict()
    items = [
        {"result_id": f"ux-resource-result-{position:03}", "title": f"Example.S01E0{position}.2160p",
         "site_id": "demo", "site_name": "Demo", "size_text": size["size_text"],
         "size_bytes": size["size_bytes"], "download_state": "ready",
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


async def _download_intent(store, view, positions=None, target="guangya", *, owner=OWNER, session=SESSION):
    from app.agent.domains.resources.selection import resource_download_arguments
    snapshot = await store.resolve(view["ref"], owner=owner, session_id=session, expected_kind="resource_candidates")
    return resource_download_arguments(snapshot, positions or [1], target)


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
            intent["result_ids"] = [f"foreign-resource-{self.forged_position}"]
        yield ModelEvent(ModelEventType.TOOL_CALL_COMPLETED,
                         tool_call=ModelToolCall("selection-preview", "resources.download", intent))
        yield ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")


def _runtime(store=None, *, ttl=900, model=None, resource_result=None, present_result=False):
    from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
    from app.agent.kernel.ports.resource_presentation import ResourceSelection
    from app.agent.kernel.ports.sdk_effects import resource_download_tool
    states = store or InMemorySessionStateStore()
    catalog = ToolCatalog([resource_download_tool()], selection=ResourceSelection())
    pipeline = ToolPipeline(catalog=catalog, state_store=states, reference_store=store)
    async def read_resources(arguments, context):
        result = resource_result if resource_result is not None else _resources(ttl=ttl)
        _cache_candidates(result.references[0].value["candidates"])
        outcome = await observe_fixture(pipeline, context, result, arguments)
        if present_result:
            identities = [item["result_id"] for item in result.data["items"]]
            return (await present(pipeline, context, identities)).outcome
        return outcome
    catalog.register(KernelToolSpec(name="fixture.resource_read", domain="fixture", effect=ToolEffect.READ,
        description="测试外部资源I/O；正式原生RPC发布", input_schema={"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]},
        read=read_resources, metadata={"publishes_selection": True}))
    session = AgentSession(model=model or SelectionModel(), catalog=catalog,
                           pipeline=pipeline, state_store=states)
    return session, pipeline, states


async def _publish_candidates(pipeline, states, *, owner=OWNER, session_id=SESSION, context=None):
    if context is None:
        lease, _ = await states.begin_turn(owner=owner, session_id=session_id, request_id="search")
        context = ToolCallContext(owner=owner, session_id=session_id, request_id="search", turn_id=lease.turn_id,
                                  lease=lease, cancellation=CancellationToken(), report_progress=AsyncMock())
    result = await pipeline.execute("fixture.resource_read", {"title": "Example"}, context=context)
    from app.agent.kernel.ports.resource_presentation import ResourcePresentation
    from app.agent.kernel.references import ResultSnapshotReader
    from app.agent.domains.resources.selection import _recommend
    from app.modules.resource_candidates import candidate_sources
    scope = await ResourcePresentation(context, pipeline, ResultSnapshotReader(pipeline.reference_store)).snapshot()
    assert scope is not None
    items = [item for source in candidate_sources(scope) for item in source["_display_items"]]
    recommended = _recommend(items)
    selected = recommended if any(item.get("requested_episode") for item in items) else [item["position"] for item in items]
    result = await present(pipeline, context, [item["result_id"] for item in items if item["position"] in selected])
    view = result.outcome.public_content["candidate_view"]
    return view, context


async def _events(stream):
    return [event async for event in stream]


def test_candidate_projection_is_allowlisted_and_current_view_restores(store):
    async def exercise():
        _, pipeline, states = _runtime(store)
        view, context = await _publish_candidates(pipeline, states)
        result = await pipeline.execute("fixture.resource_read", {"title": "Example"}, context=context)
        assert result.outcome.public_content["candidate_view"] is None
        presented = await present(pipeline, context, [item["result_id"] for item in model_resource_candidates(json.loads(result.outcome.model_content))])
        view = presented.outcome.public_content["candidate_view"]
        public = json.dumps(presented.outcome.public_content, ensure_ascii=False)
        assert [item["result_id"] for item in view["items"]] == ["ux-resource-result-001", "ux-resource-result-002"]
        model = json.loads(result.outcome.model_content)
        assert model["data"]["items"][0]["result_id"] == "ux-resource-result-001"
        assert model["data"]["items"][0]["size_text"] == view["items"][0]["size_text"] == "359.69 MiB"
        encoded = public + result.outcome.model_content
        for private in ("rawresult", "torrent_url", "SECRET", "10.0.0.9", "/private/", "https://"):
            assert private not in encoded
        assert set(view) == {"ref", "selection_ref", "expires_at", "items", "turn_id", "recommended_positions", "target", "target_source", "targets", "explicit_selection"}
        assert view["expires_at"] > time.time()
        item = view["items"][0]
        assert set(item) == {"position", "result_id", "title", "site_name", "size_text", "tags", "reasons", "warnings", "coverage", "media_title", "media_scope", "requested_episode", "match"}
        # 展示只信引用内的安全快照，不从早先原始结果恢复额外字段。
        assert item["tags"] == {"audio": "Atmos", "resolution": "2160p"}
        assert item["reasons"] == ["精确匹配"] and item["warnings"] == ["需要人工确认"]
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
             patch("app.modules.resource_candidates.restore_resource_candidate_reference", side_effect=AssertionError("restore forbidden")) as restore:
            events = await _events(session.run(AgentInput(message="选择并预览", owner=owner, session_id=session_id, metadata={"selection": value})))
        assert len(events) == 1 and events[0].type is AgentEventType.TURN_FAILED
        assert events[0].payload["code"] == "selection_invalid"
        assert isinstance(session.model, SelectionModel)
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
        with patch("app.indexers.downloads.preview_indexer_download_batch") as prepare, \
             patch("app.indexers.downloads.submit_indexer_download_batch") as execute:
            from tests.support import _prepare_batch
            prepare.side_effect = _prepare_batch
            execute.return_value = {"ok": True, "status": "accepted", "summary": "已提交", "error": "",
                                    "total": 1, "succeeded": 1, "failed": 0,
                                    "items": [{"result_id": "ux-resource-result-001", "status": "submitted", "request_id": 81}]}
            events = await _events(session.run(envelope.to_agent_input()))
            approvals = [event for event in events if event.type is AgentEventType.EFFECT_APPROVAL_REQUIRED]
            assert len(approvals) == 1, [(event.type, event.payload) for event in events]
            prepare.assert_called_once()
            execute.assert_not_called()
            assert isinstance(session.model, ScriptedModel)
            assert session.model.requests == []
            assert prepare.call_args.args[2] == "guangya"
            state = await states.load(owner=OWNER, session_id=SESSION)
            pending = state.pending_effect_plan_id
            assert pending == approvals[0].payload["plan"]["plan_id"]
            stale = await _events(session.run(envelope.to_agent_input()))
            assert stale[0].payload["code"] == "selection_invalid"
            assert (await states.load(owner=OWNER, session_id=SESSION)).pending_effect_plan_id == pending
            assert session.model.requests == []
            first = await _events(session.confirm(owner=OWNER, session_id=SESSION, plan_id=pending))
            assert any(event.type is AgentEventType.EFFECT_COMPLETED for event in first)
            receipt = next(event.payload["result"]["data"] for event in first if event.type is AgentEventType.EFFECT_COMPLETED)
            assert (receipt["total"], receipt["items"][0]["request_id"]) == (1, 81)
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
        with patch("app.indexers.downloads.preview_indexer_download_batch") as prepare:
            for arguments in ({**verified.arguments, "positions": [2]},
                              {**verified.arguments, "positions": [True]},
                              {**verified.arguments, "target": "both"}):
                with pytest.raises(ToolPipelineError, match="只能预览"):
                    await pipeline.execute("resources.download", arguments, context=context)
            with pytest.raises(ToolPipelineError, match="只能预览"):
                await pipeline.execute("fixture.resource_read", {"title": "重新搜索"}, context=context)
            prepare.assert_not_called()
    asyncio.run(exercise())


def test_pending_preview_restores_activity_without_executing_or_changing_plan(api):
    _login(api)
    owner = _web_owner(api)
    session = api.runtime.session
    view, _ = asyncio.run(_publish_candidates(session.pipeline, api.store, owner=owner))
    from tests.support import _prepare_batch
    with patch("app.indexers.downloads.preview_indexer_download_batch", side_effect=_prepare_batch), \
         patch("app.indexers.downloads.submit_indexer_download_batch") as execute:
        events = asyncio.run(_events(session.run(QueryEnvelope(
            owner=owner, session_id=SESSION, message="选择并预览", selection=_selection(view),
        ).to_agent_input())))
        approval = next(event.payload["plan"] for event in events
                        if event.type is AgentEventType.EFFECT_APPROVAL_REQUIRED)
        before = asyncio.run(api.store.load(owner=owner, session_id=SESSION))
        frozen = asyncio.run(session.pipeline.get_pending_plan(owner=owner, session_id=SESSION))
        assert frozen.plan_id == approval["plan_id"]
        response = api.client.get(f"/api/agent/sessions/{SESSION}")
        assert response.status_code == 200, response.text
        restored = response.json()["pending_approval"]
        activities = restored.pop("activities")
        assert restored == frozen.public_approval_dict()
        assert activities[-1]["method"] == "resources.download"
        assert activities[-1]["status"] == "approval_required"
        assert activities[-1]["label"]
        assert asyncio.run(api.store.load(owner=owner, session_id=SESSION)) == before
        execute.assert_not_called()


def test_get_session_restores_only_valid_current_candidates_and_query_accepts_selection(api):
    _login(api)
    owner = _web_owner(api)
    pipeline = api.runtime.session.pipeline
    view, _ = asyncio.run(_publish_candidates(pipeline, api.store, owner=owner))
    response = api.client.get(f"/api/agent/sessions/{SESSION}")
    assert response.status_code == 200 and response.json()["candidate_view"] == view
    assert response.json()["scoped_trust"] == {"active": False}
    class Web:
        async def activity(self, *, owner, session_id):
            return None

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
    response = api.client.get(f"/api/agent/sessions/{SESSION}")
    assert response.status_code == 200 and response.json()["candidate_view"] is None
    assert response.json()["scoped_trust"] == {"active": False}


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




@pytest.mark.parametrize("rejection", ["arguments", "rate_limit"])
def test_pre_read_rejection_keeps_current_card_and_emits_no_invalidation(store, rejection):
    async def exercise():
        _, pipeline, states = _runtime(store)
        view, context = await _publish_candidates(pipeline, states)
        assert isinstance(context.report_progress, AsyncMock)
        context.report_progress.reset_mock()
        await states.commit(context.lease, updates=(StateUpdate("pending_effect_plan_id", "keep-pending"),))
        before = await states.load(owner=OWNER, session_id=SESSION)
        if rejection == "rate_limit":
            pipeline.rate_limiter.acquire = AsyncMock(side_effect=ToolPipelineError("limited", code="rate_limited"))
        with pytest.raises(ToolPipelineError):
            await pipeline.execute("fixture.resource_read", {} if rejection == "arguments" else {"title": "new search"}, context=context)
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
    from app.agent.domains.resources.selection import _recommend, candidate_item

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
    from app.agent.domains.resources.selection import _recommend, candidate_item
    items = [candidate_item({"title": "同名剧.S01E01", "match": "exact_episode", "_verification_context": {
        "title": "同名剧", "tmdb_id": str(identity), "season": 1, "episode": 1,
    }}, position) for position, identity in enumerate((111, 222), 1)]
    assert _recommend(items) == [1, 2]
    assert [item["media_scope"] for item in items] == ["tmdb:111", "tmdb:222"]


def test_general_search_does_not_recommend_old_episodes_or_suggest_ingest(store):
    async def exercise():
        _, pipeline, states = _runtime(store)
        view, context = await _publish_candidates(pipeline, states)
        result = await pipeline.execute("fixture.resource_read", {"title": "Example"}, context=context)
        assert view["items"], "普通搜索仍应允许用户手动挑选"
        assert view["recommended_positions"] == []
        assert '"recommended_download_arguments":' not in result.outcome.model_content
        assert model_resource_candidates(json.loads(result.outcome.model_content))
        view, _ = await _publish_candidates(pipeline, states, context=context)
        # 旧会话曾按标题给出自动推荐，重新读取时不能继续沿用。
        state = await states.load(owner=OWNER, session_id=SESSION)
        state.metadata[CANDIDATE_VIEW_KEY]["recommended_positions"] = [1]
        restored = await current_candidate_view(state=state, store=store)
        assert restored is not None
        assert restored["recommended_positions"] == []
        selection = await validate_selection(_selection(restored), state=state, store=store)
        assert selection.arguments["result_ids"] == [restored["items"][0]["result_id"]]
    asyncio.run(exercise())


@pytest.mark.parametrize("match", ["", "unknown", "season_pack", "conflict"])
def test_missing_episode_context_alone_does_not_prove_candidate_coverage(match):
    from app.agent.domains.resources.selection import _recommend, candidate_item

    item = candidate_item({
        "title": "[GM-Team][东大高武学院][01-04][4K]", "match": match,
        "_verification_context": {"title": "东大高武学院", "season": 1, "episode": 9},
    }, 1)
    assert _recommend([item]) == []
    assert _recommend([]) == []


def test_verified_match_recommendations_use_domain_evidence_not_generic_title_ranges():
    from app.agent.domains.resources.selection import _recommend, candidate_item

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
            result = await pipeline.execute("fixture.resource_read", {"title": "仙逆"}, context=context)
        assert result.outcome.public_content["candidate_view"] is None
        assert result.outcome.public_content["summary"] == "已检索,缺集覆盖仍需核对"
        assert model_resource_candidates(json.loads(result.outcome.model_content))
        assert '"recommended_download_arguments":' not in result.outcome.model_content
        state = await states.load(owner=OWNER, session_id=SESSION)
        assert state.metadata[CANDIDATE_VIEW_KEY] is None
        assert await current_candidate_view(state=state, store=store) is None
    asyncio.run(exercise())


@pytest.mark.parametrize("legacy_empty", [False, True])
def test_restored_empty_or_unverified_episode_card_is_not_resurrected(store, legacy_empty):
    from app.agent.domains.resources.selection import candidate_item

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


def test_mixed_episode_candidates_keep_verified_identity_in_selected_card(store):
    async def exercise():
        _, pipeline, states = _runtime(store)
        with patch(__name__ + "._resources", return_value=_episode_candidates_result(include_match=True)):
            view, _ = await _publish_candidates(pipeline, states)
        assert view["recommended_positions"] == [1]
        assert [item["position"] for item in view["items"]] == [1]
        state = await states.load(owner=OWNER, session_id=SESSION)
        current_candidate_view_result = await current_candidate_view(state=state, store=store)
        assert current_candidate_view_result is not None
        assert (current_candidate_view_result)["recommended_positions"] == [1]
        result = await validate_selection(_selection(view, [1]), state=state, store=store)
        assert result.arguments["result_ids"] == [view["items"][0]["result_id"]]
    asyncio.run(exercise())


def test_raw_search_keeps_model_evidence_without_publishing_unselected_cards(store):
    async def exercise():
        _, pipeline, states = _runtime(store)
        lease, _ = await states.begin_turn(owner=OWNER, session_id=SESSION, request_id="remaining-resources")
        context = ToolCallContext(owner=OWNER, session_id=SESSION, request_id="remaining-resources", turn_id=lease.turn_id,
                                  lease=lease, cancellation=CancellationToken(), report_progress=AsyncMock())
        for title in ("狐妖小红娘", "Fox Spirit Matchmaker"):
            result = await pipeline.execute("fixture.resource_read", {"title": title}, context=context)
            assert result.outcome.public_content["candidate_view"] is None
            assert model_resource_candidates(json.loads(result.outcome.model_content))
            assert {item["result_id"] for item in model_resource_candidates(json.loads(result.outcome.model_content))} == {"ux-resource-result-001", "ux-resource-result-002"}
            assert '"recommended_download_arguments":' not in result.outcome.model_content
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


def test_explicit_presentation_numbers_selected_ids_and_can_clear_the_view(store):
    async def exercise():
        _, pipeline, states = _runtime(store)
        original, context = await _publish_candidates(pipeline, states)
        result = await present(pipeline, context, [original["items"][1]["result_id"]])
        view = result.outcome.public_content["candidate_view"]
        assert [item["position"] for item in view["items"]] == [1]
        assert view["explicit_selection"] is True
        assert view["recommended_positions"] == []
        state = await states.load(owner=OWNER, session_id=SESSION)
        current_candidate_view_result_2 = await current_candidate_view(state=state, store=store)
        assert current_candidate_view_result_2 is not None
        assert (current_candidate_view_result_2)["items"] == view["items"]
        assert (await validate_selection(_selection(view, [1]), state=state, store=store)).arguments["result_ids"] == [view["items"][0]["result_id"]]
        with pytest.raises(SelectionInvalidError):
            await validate_selection(_selection(view, [2]), state=state, store=store)
        cleared = await present(pipeline, context, [])
        assert cleared.outcome.public_content["candidate_view"] is None
        assert not json.loads(cleared.outcome.model_content).get("candidate_numbers")
        state = await states.load(owner=OWNER, session_id=SESSION)
        assert await current_candidate_view(state=state, store=store) is None
    asyncio.run(exercise())


def test_candidate_refinement_filters_model_card_and_selection_from_one_result(store, monkeypatch):
    resources = _resources()
    resources.data["items"][1]["title"] += ".SDR"
    resources.references = [ToolReference("resource_candidates", safe_resource_snapshot(resources, search_id=new_resource_search_id()))]
    monkeypatch.setattr(__import__(__name__, fromlist=["_resources"]), "_resources", lambda **_: resources)

    async def exercise():
        _, pipeline, states = _runtime(store)
        original, context = await _publish_candidates(pipeline, states)
        result = await present(pipeline, context, [item["result_id"] for item in original["items"]], resolution="2160p", dynamic_range="SDR")
        model = json.loads(result.outcome.model_content)
        view = result.outcome.public_content["candidate_view"]
        assert model["result_ids"] == [original["items"][1]["result_id"]]
        assert model["technical_filter"]["unknown_excluded"] == 1
        assert model["scope"]["complete"] is True
        assert model["result_ids"] == [view["items"][0]["result_id"]]
        assert [item["position"] for item in view["items"]] == [1]
        state = await states.load(owner=OWNER, session_id=SESSION)
        current_candidate_view_result_3 = await current_candidate_view(state=state, store=store)
        assert current_candidate_view_result_3 is not None
        assert (current_candidate_view_result_3)["items"] == view["items"]
        assert (await validate_selection(_selection(view, [1]), state=state, store=store)).arguments["result_ids"] == [view["items"][0]["result_id"]]
        with pytest.raises(SelectionInvalidError):
            await validate_selection(_selection(view, [2]), state=state, store=store)

        empty = await present(pipeline, context, [item["result_id"] for item in original["items"]], dynamic_range="HDR")
        model = json.loads(empty.outcome.model_content)
        assert model["result_ids"] == []
        assert not model.get("candidate_numbers")
        assert empty.outcome.public_content["candidate_view"] is None
        assert await current_candidate_view(state=await states.load(owner=OWNER, session_id=SESSION), store=store) is None
        # 筛选不破坏原快照，也不把当前展示作为隐藏的永久检索条件。
        unrestricted = await present(pipeline, context, [item["result_id"] for item in original["items"]])
        assert len(unrestricted.outcome.public_content["candidate_view"]["items"]) == 2

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
                    f"search-{len(self.requests)}", "fixture.resource_read",
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






def test_explicit_sdk_query_contract_survives_empty_results_and_json_reload(tmp_path, monkeypatch):
    """查询事实显式存文件；不保留已退役的普通搜索引用续查协议。"""
    from app import sdk
    from app.indexers.ranking import filter_technical_candidates
    from tests.test_agent_indexer_actions import FakeIndexerService, _resource_item

    service = FakeIndexerService(items=[], partial=False)
    monkeypatch.setattr("app.indexers.runtime.get_indexer_service", lambda: service)
    path = tmp_path / "query.json"
    query = {"title": "Example", "season": 1, "episode": 1, "resolution": "1080p", "media_type": "tv"}
    result = None
    for turn, (action, change) in enumerate((
        ("search", {}), ("search", {"season": 2}), ("search", {"resolution": "2160p"}),
        ("filter", {"exclude_hdr": True}), ("search", {"season": 3}),
        ("filter", {"dynamic_range": "SDR"}), ("search", {"resolution": "1080p"}), ("filter", {}),
    ), 1):
        if path.exists():
            query = json.loads(path.read_text())
        query.update(change)
        path.write_text(json.dumps(query))
        if turn == 7:
            service.items = [_resource_item(title="Example.S03E01.1080p.SDR")]
        if action == "search":
            result = sdk.resources.search(**json.loads(path.read_text()))
            assert result["request"]["season"] == query["season"]
            assert result["request"]["episode"] == query["episode"]
        else:
            assert result is not None
            result["items"], result["technical_filter"] = filter_technical_candidates(result["items"], query)
        assert result["technical_filter"]["requested"]["resolution"] == query["resolution"]
        if turn < 7:
            assert result["items"] == [] and result["recommendation"]["selected"] is None
        if turn >= 4:
            assert result["technical_filter"]["requested"]["exclude_hdr"] is True
        if turn >= 6:
            assert result["technical_filter"]["requested"]["dynamic_range"] == "SDR"
        if turn == 8:
            assert len(result["items"]) == 1
            assert result["items"][0]["technical_evidence"]["dynamic_range"] == "SDR"
    assert len(service.search_calls) == 5  # 本地过滤不重新搜索。
