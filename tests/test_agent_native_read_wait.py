"""默认模型入口经真实 bwrap/SDK 额度拒绝后，只恢复被冻结的读取。"""
from __future__ import annotations

import asyncio
import json
import threading
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace

import pytest

from app import config, sdk
from app.agent import rate_limit
from app.agent.feature_gate import initialize_runtime_fence
from app.agent.kernel import sandbox
from app.agent.kernel.bootstrap import (
    _capture_creation_binding,
    build_agent_kernel_runtime,
)
from app.agent.kernel.model import ModelEvent, ModelEventType, ModelToolCall
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.sandbox import _workspace_location
from app.agent.kernel.state_types import AgentInput
from app.agent.owner_routes import web_kernel_owner
from app.agent.transports import QueryEnvelope
from tests import test_agent_generation_profiles as profiles
from tests.test_agent_sdk_effects import (
    native_library_backend as native_library_backend,
)
from tests.test_guangya_read_contracts import ReadClient

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


@pytest.mark.parametrize("channel", ["web", "telegram"])
@pytest.mark.parametrize("bound", [True, False], ids=["with-scope", "without-scope"])
def test_foreground_directory_listing_stops_at_first_quota_denial(local_config, monkeypatch, channel, bound):
    owner = OWNER if channel == "web" else "tg:v1:-1001\x1f7"
    route = {} if channel == "web" else {"chat_id": "-1001", "message_thread_id": 9}
    if route:
        config.set_and_save({"TG_CHAT_ID": "-1001", "TG_AGENT_ALLOWED_USER_IDS": "7", "TG_AGENT_ENABLED": "1"})
    clock = [100]
    monkeypatch.setitem(rate_limit._TOOL_RATE_LIMITS, "cloud.list", 1)
    monkeypatch.setattr(rate_limit.agent_rate_limiter, "_wall_clock", lambda: clock[0])
    client = ReadClient({"data": {"total": 1, "list": [{"fileId": "dir-1", "fileName": "动漫", "resType": 2}]}})
    monkeypatch.setattr(client, "close", lambda: True)
    monkeypatch.setattr("app.clients.guangya.GuangYaClient", lambda: client)
    code = (
        "from pathlib import Path\n"
        "root = mf.cloud.list()\n"
        "print(root['items'][0]['name'])\n"
        "Path('root.txt').write_text(root['items'][0]['name'])\n"
        "for folder in ('动漫', '剧集', '综艺', '电影', '纪录片'):\n"
        "    try:\n"
        "        mf.cloud.list('/' + folder)\n"
        "    except mf.Error as error:\n"
        "        print(folder, error.code)\n"
        "Path('tail-ran').touch()\n"
    )

    async def run():
        model = NativeModel([code])
        active = runtime(model, clock)
        if not bound:
            active.session.continuation_binding = None
        events = [event async for event in active.session.run(AgentInput(
            message="列出光鸭根目录并核对各分类。", owner=owner, session_id=SESSION,
            request_id="foreground-list", channel=channel, delivery_route=route))]
        assert len(model.requests) == 2
        state = await active.store.load(owner=owner, session_id=SESSION)
        assert not state.metadata.get("task")
        assert not state.metadata.get("read_wait")
        assert events[-1].type.value == "turn.completed" and events[-1].payload["status"] != "waiting"
        assert await active.store.due_continuations() == []
        activities = [event.payload["activity"] for event in events
                      if (event.payload.get("activity") or {}).get("method") == "cloud.list"]
        assert [activity["summary"] for activity in activities if activity["status"] == "failed"] == ["已达读取额度"]
        assert [activity["summary"] for activity in activities if activity["status"] == "success"] == ["根目录 · 本次返回 1 项"]
        receipt = json.loads(next(row.content for row in model.requests[1].messages if row.role == "tool"))
        assert receipt["status"] == "rate_limited" and receipt["ok"] is False
        data = receipt["data"]
        assert data["stopped_by"] == "read_quota"
        assert data["exit_code"] != 0 and data["timed_out"] is False
        assert data["stdout"].strip() == "动漫"
        completed, denied = data["sdk_calls"]
        assert all(call["method"] == "cloud.list" for call in (completed, denied))
        assert completed["ok"] is True
        assert denied["status"] == "rate_limited" and denied["execution_started"] is False
        assert denied["rate_limit"]["reason"] == "quota" and denied["rate_limit"]["retry_after_seconds"] > 0
        stored = json.loads(next(row["content"] for row in state.conversation if row.get("tool_call_id") == "native-1"))
        assert stored["status"] == "rate_limited" and stored["data"]["sdk_calls"] == data["sdk_calls"]
        client.raw.fs_files.assert_called_once()
        root, _ = _workspace_location(owner, SESSION)
        archived = json.loads((root / completed["response_path"].lstrip("/")).read_text())
        assert [item["name"] for item in archived["result"]["items"]] == ["动漫"]
        assert (root / "work/root.txt").read_text() == "动漫"
        assert not (root / "work/tail-ran").exists()

    asyncio.run(run())


def test_directory_activities_keep_safe_errors_and_distinguish_returned_paths(local_config, monkeypatch):
    directories = [{"fileId": "10", "fileName": "动漫", "resType": 2},
                   {"fileId": "20", "fileName": "剧集", "resType": 2}]
    client = ReadClient(None)
    transient_failures = 2

    def files(*, parent_id, **_kwargs):
        nonlocal transient_failures
        if parent_id == "20" and transient_failures:
            transient_failures -= 1
            raise TimeoutError("光鸭云盘读取超时，请稍后重试")
        items = directories if parent_id is None else []
        return {"data": {"total": len(items), "list": items}}

    client.raw.fs_files.side_effect = files
    monkeypatch.setattr(client, "close", lambda: True)
    monkeypatch.setattr(client, "_read_retry_delay", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr("app.clients.guangya.GuangYaClient", lambda: client)
    magnet = "magnet:?xt=urn:btih:" + "a" * 40
    token = "access_token=offline-directory-token"
    code = (
        "try:\n"
        f"    mf.cloud.list('/动漫', offset=-1, query={magnet!r})\n"
        "except mf.Error as error:\n"
        "    print(error.code)\n"
        "try:\n"
        "    mf.cloud.list('/剧集')\n"
        "except mf.Error as error:\n"
        "    print(error.code)\n"
        f"first = mf.cloud.list('/动漫', query={magnet!r})\n"
        f"second = mf.cloud.list('/剧集', query={token!r})\n"
        "print(first['root']['path'], second['root']['path'])\n"
    )

    async def run():
        model = NativeModel([code])
        active = runtime(model, [100])
        events = [event async for event in active.session.run(AgentInput(
            message="分别盘点这两个归档目录。", owner=OWNER, session_id=SESSION,
            request_id="directory-summaries", channel="web"))]
        activities = [event.payload["activity"] for event in events
                      if (event.payload.get("activity") or {}).get("method") == "cloud.list"]
        assert [row["summary"] for row in activities if row["status"] == "failed"] == [
            "分页或扫描预算无效", "光鸭云盘读取超时,请稍后重试",
        ]
        assert [row["summary"] for row in activities if row["status"] == "success"] == [
            "动漫 · 本次返回 0 项", "剧集 · 本次返回 0 项",
        ]
        summaries = "\n".join(row["summary"] for row in activities)
        assert "magnet:" not in summaries and "offline-directory-token" not in summaries
        receipt = json.loads(next(row.content for row in model.requests[1].messages if row.role == "tool"))
        assert receipt["status"] == "success" and receipt["data"]["exit_code"] == 0
        assert "stopped_by" not in receipt["data"]
        assert receipt["data"]["stdout"].splitlines() == ["GuangYaWorkspaceError", "TimeoutError", "/动漫 /剧集"]
        assert [call["ok"] for call in receipt["data"]["sdk_calls"]] == [False, False, True, True]

    asyncio.run(run())


def test_followup_progress_identifies_requested_record_and_scoped_pages(local_config, monkeypatch):
    from tests.test_rss_delivery_native import entry, feed

    sid = feed()
    ids = [entry(sid, i) for i in (1, 2)]
    client = ReadClient({})
    monkeypatch.setattr(client, "file_info", lambda _: None)
    monkeypatch.setattr(client, "close", lambda: True)
    monkeypatch.setattr("app.clients.guangya.GuangYaClient", lambda: client)
    code = (f"mf.rss.entries(entry_ids={ids}, limit=1)\n"
            "mf.activity.timeline('download', 987654321)\n"
            "mf.cloud.info('501')\n")

    async def run():
        active = runtime(NativeModel([code]), [100])
        events = [event async for event in active.session.run(AgentInput(
            message="按这两条回执、下载编号和云盘文件分别核对。", owner=OWNER, session_id=SESSION, channel="web"))]
        activities = [activity for event in events if (activity := event.payload.get("activity"))
                      and activity.get("status") != "running"]
        summaries = {item["method"]: item["summary"] for item in activities}
        assert "本次返回 1 项" in summaries["rss.entries"] and "还有后续页" in summaries["rss.entries"]
        assert "下载请求 #987654321" in summaries["activity.timeline"]
        assert summaries["cloud.info"] == "未找到该文件或目录"

    asyncio.run(run())


def workspace_web_receipt(code):
    async def run():
        model = NativeModel([code])
        active = runtime(model, [100])
        events = [json.loads(chunk) async for chunk in active.web.query(QueryEnvelope(
            message="核对实际查询范围。", owner=OWNER, session_id=SESSION, channel="web"))]
        assert events[-1]["type"] == "turn.completed"
        receipt = json.loads(next(row.content for row in model.requests[1].messages if row.role == "tool"))
        assert receipt["data"]["exit_code"] == 0, receipt["data"]["stderr"]
        activities = [activity for event in events if event["type"] == "tool.progress"
                      and (activity := event["payload"].get("activity")) and activity["status"] != "running"]
        return receipt["data"], activities

    return asyncio.run(run())


def test_workspace_library_scope_follows_real_inventory_cutoff_and_batch(local_config, monkeypatch):
    from app import services
    from app.clients.base import (
        MediaItem,
        SeriesCandidate,
        SeriesEpisodeInventory,
        SeriesSearchResult,
    )
    from app.modules import episode_audit, media_identity, media_library

    class MediaServer:
        def list_virtual_folders(self):
            return [{"id": "private-folder-a", "name": "A库"}, {"id": "private-folder-b", "name": "B库"}]

        def search_series_candidates(self, query, *, limit, parent_id=""):
            by_library = {
                "private-folder-a": [SeriesCandidate("series-a", "同名剧", "2025", "100")],
                "private-folder-b": [SeriesCandidate("series-b", "同名剧", "2020", "101"),
                                     SeriesCandidate("series-c", "海角", "2025", "102")],
            }
            rows = by_library[parent_id] if parent_id else by_library["private-folder-a"] + by_library["private-folder-b"]
            matches = [row for row in rows if row.name == query]
            return SeriesSearchResult(matches, len(matches))

        def list_series_episode_inventory(self, series_id, **_kwargs):
            numbers = (1, 2) if series_id == "series-b" else (1,)
            return SeriesEpisodeInventory([(1, number) for number in numbers], len(numbers))

        def close(self):
            pass

    class TMDB:
        def detail(self, tmdb_id, _media_type):
            return {"name": "同名剧" if tmdb_id == "100" else "海角", "first_air_date": "2025-07-01",
                    "seasons": [{"season_number": 1, "episode_count": 2}]}

        def tv_season_detail(self, _tmdb_id, _season):
            return {"episodes": [{"episode_number": i, "air_date": f"2025-07-0{i}"} for i in (1, 2)]}

    client = MediaServer()
    monkeypatch.setattr(media_identity, "datetime", SimpleNamespace(now=lambda: datetime(2025, 7, 2, 12)))
    monkeypatch.setattr(services, "_configured_media_sources", lambda: [("jellyfin", "Jellyfin", "", client)])
    monkeypatch.setattr(media_library, "_profile", lambda _server: SimpleNamespace(
        server_type="emby", create_client=lambda: nullcontext(client)))
    monkeypatch.setattr(episode_audit, "TMDBClient", TMDB)
    monkeypatch.setattr(episode_audit, "close_tmdb_client", lambda _client: None)
    monkeypatch.setattr(episode_audit, "search_media_servers", lambda *_args, **_kwargs: [{
        "server_type": "jellyfin", "has_more": False, "items": [MediaItem("movie-1", "雨夜", "movie")],
    }])

    data, progress = workspace_web_receipt(
        "print(mf.library.episodes('同名剧', library_name=' Ａ库 ')['data']['local_episode_count'])\n"
        "print(mf.library.episodes('同名剧', library_name='B库')['data']['local_episode_count'])\n"
        "print(mf.library.episodes('同名剧')['data']['local_episode_count'])\n"
        "print(mf.library.episodes(series_id='series-b')['data']['count'])\n"
        "print(mf.library.audit('同名剧', library_name='A库', season=1, as_of=' 2025-07-01 ')['data']['missing_count'])\n"
        "print(mf.library.audit('同名剧', library_name='A库', season=1)['data']['missing_count'])\n"
        "print(mf.library.updates('海角', media_type='tv', as_of='2025-07-01')['data']['missing_count'])\n"
        "print(len(mf.library.updates(queries=['同名剧', '海角', '海角'], media_type='tv')['data']['items']))\n"
        "print(len(mf.library.updates(queries=['雨夜'], media_type='movie', as_of='2025-07-01')['data']['items']))\n"
        "print(mf.library.episodes('同名剧', library_name='access_token=private-library')['data']['local_episode_count'])\n"
        "try:\n"
        "    mf.library.audit('同名剧', library_name='A库', as_of='access_token=private-cutoff')\n"
        "except mf.Error:\n"
        "    pass\n"
    )
    assert data["stdout"].splitlines() == ["1", "2", "None", "2", "0", "1", "0", "2", "1", "None"]
    a, b, across, series, early, default, updates, batch, movie, private, rejected = data["sdk_calls"]
    inventory_only = {"as_of": None, "as_of_source": "not_applicable"}
    assert a["scope"] == {"library_scope": "named", "library_name": "A库", **inventory_only}
    assert b["scope"] == {"library_scope": "named", "library_name": "B库", **inventory_only}
    assert across["ok"] is False and across["status"] == "ambiguous"
    assert across["scope"] == {"library_scope": "all_configured", **inventory_only}
    assert series["scope"] == {"library_scope": "series", "server": "emby", **inventory_only}
    assert early["scope"] == {"library_scope": "named", "library_name": "A库", "as_of": "2025-07-01", "as_of_source": "specified"}
    assert default["scope"] == {**early["scope"], "as_of": "2025-07-02", "as_of_source": "default"}
    assert updates["scope"] == {"library_scope": "all_configured", "as_of": "2025-07-01", "as_of_source": "specified"}
    assert batch["status"] == "partial"
    assert batch["scope"] == {"library_scope": "all_configured", "batch_size": 2,
                              "as_of": "2025-07-02", "as_of_source": "default"}
    assert movie["scope"] == {"library_scope": "all_configured", "batch_size": 1, **inventory_only}
    assert private["status"] == "library_not_found"
    assert private["scope"] == {"library_scope": "named", "library_name": None, **inventory_only}
    assert rejected["ok"] is False and rejected["scope"] is None
    assert [row["status"] for row in progress] == [
        "success", "success", "failed", "success", "success", "success", "success", "partial", "partial", "failed", "failed",
    ]
    expected_scopes = ("A库", "B库", "未指定库", "Emby", "2025-07-01", "2025-07-02", "未指定库", "批量 2 项",
                       "无播出截止日", "名称不公开", "查询范围未确认")
    for row, expected in zip(progress, expected_scopes, strict=True):
        assert expected in row["summary"]
    assert "默认" in progress[5]["summary"] and "未指定库" in progress[7]["summary"]
    for private_value in ("private-folder", "private-library", "private-cutoff"):
        assert private_value not in json.dumps([data, progress])


def test_workspace_resource_scope_uses_normalized_year_and_actual_site_results(local_config, monkeypatch):
    from app.indexers import runtime as indexer_runtime
    from app.indexers.models import IndexerCapabilities, IndexerItem, IndexerPage
    from app.indexers.providers.base import DirectResultAdapter
    from app.indexers.registry import IndexerRegistry
    from app.indexers.result_store import IndexerResultStore
    from app.indexers.service import IndexerService

    class Site(DirectResultAdapter):
        default_enabled = True
        capabilities = IndexerCapabilities(True)

        def __init__(self, site_id):
            self.site_id = self.site_name = site_id

        async def search(self, request):
            if self.site_id == "mikan":
                raise TimeoutError("offline fixture timeout")
            items = [IndexerItem(self.site_id, self.site_name, f"同名剧 2024 {specs}")
                     for specs in ("1080p SDR", "2160p HDR10")]
            return IndexerPage(items, request.page, False, True)

    sites = ("fixture-a", "mikan")
    service = IndexerService(registry=IndexerRegistry({site: Site(site) for site in sites}), result_store=IndexerResultStore())
    monkeypatch.setattr(indexer_runtime, "get_indexer_service", lambda: service)
    try:
        data, progress = workspace_web_receipt(
            "print(len(mf.resources.search('同名剧', year=' ２０２４ ', sites=[' Fixture-A ', 'FIXTURE-A'], "
            "resolution='1080p', dynamic_range='SDR')['items']))\n"
            "print(len(mf.resources.search('同名剧')['items']))\n"
            "print(len(mf.resources.search('同名剧', sites=['fixture-a'], resolution='480p', exclude_hdr=True)['items']))\n"
            "print(len(mf.resources.search('同名剧', year=2020, sites=['mikan'])['items']))\n"
            "try:\n"
            "    mf.resources.search('同名剧', sites=['access_token=private-site'])\n"
            "except mf.Error:\n"
            "    pass\n"
        )
    finally:
        asyncio.run(service.aclose())
        indexer_runtime._stop_standalone_runtime()

    assert data["stdout"].splitlines() == ["1", "2", "0", "0"]
    exact, default, empty, unavailable, rejected = data["sdk_calls"]
    filter_evidence = {"basis": "release_title_only", "stream_verified": False}
    site_scope = {"site_scope": "selected", "sites_attempted": ["fixture-a"], "sites_succeeded": ["fixture-a"]}
    assert exact["scope"] == {**site_scope, "year": 2024, "technical_filter": {
        "requested": {"resolution": "1080p", "dynamic_range": "SDR"}, **filter_evidence}}
    assert default["scope"] == {"year": None, "site_scope": "default", "sites_attempted": list(sites), "sites_succeeded": ["fixture-a"]}
    assert empty["scope"] == {**site_scope, "year": None, "technical_filter": {
        "requested": {"resolution": "480p", "exclude_hdr": True}, **filter_evidence}}
    assert unavailable["scope"] == {"year": 2020, "site_scope": "selected", "sites_attempted": ["mikan"], "sites_succeeded": []}
    assert rejected["scope"] is None
    assert [row["status"] for row in progress] == ["success", "partial", "success", "partial", "failed"]
    assert all(text in progress[0]["summary"] for text in ("2024", "fixture-a", "规格筛选条件 1080p、SDR", "未核验文件"))
    assert "未指定年份" in progress[1]["summary"] and "未指定站点" in progress[1]["summary"]
    assert "规格筛选条件 480p、排除已标 HDR" in progress[2]["summary"] and "返回 0 项" in progress[2]["summary"]
    assert "2020" in progress[3]["summary"] and "mikan" in progress[3]["summary"] and "未取得结果" in progress[3]["summary"]
    assert "查询范围未确认" in progress[4]["summary"]
    assert "private-site" not in json.dumps(data) and "private-site" not in json.dumps(progress)


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
    import fcntl

    clock = [100]
    monkeypatch.setattr(rate_limit.agent_rate_limiter, "_wall_clock", lambda: clock[0])
    entered, release = threading.Event(), threading.Event()
    original = sandbox.METHODS["diagnostics.component"]
    backend_calls = []
    root, _ = _workspace_location(OWNER, SESSION)
    gate = root / "work/quota-backend-entered.lock"

    def read(component):
        assert component == "qbittorrent", "被拒绝的读取不能进入后端"
        backend_calls.append(component)
        entered.set()
        fcntl.flock(backend_entered, fcntl.LOCK_UN)
        assert release.wait(5)
        return original(component)

    monkeypatch.setitem(sandbox.METHODS, "diagnostics.component", read)
    query = "tmdb" if mode == "different_query" else "qbittorrent"
    code = ("import fcntl, json\nfrom concurrent.futures import ThreadPoolExecutor\n"
            "with ThreadPoolExecutor(2) as pool:\n"
            "    first = pool.submit(mf.diagnostics.component, 'qbittorrent')\n"
            f"    with open('/work/{gate.name}') as signal:\n"
            "        fcntl.flock(signal, fcntl.LOCK_SH)\n"
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
        assert result["data"]["exit_code"] == 0
        assert result["data"]["stderr"] == ""
        completed = next(call for call in calls if call["ok"] is True)
        archived = json.loads((root / completed["response_path"].lstrip("/")).read_text())
        assert archived["result"]["data"]["component"] == "qbittorrent"
        assert archived["result"]["ok"] is True
        assert await active.store.due_continuations() == []
    gate.parent.mkdir(parents=True)
    with gate.open("wb") as backend_entered:
        # 子端的 /work 是同一目录的绑定挂载，先进入后端的读负责放行第二条 RPC。
        fcntl.flock(backend_entered, fcntl.LOCK_EX)
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


def test_unrestorable_large_sdk_arguments_stop_without_parking(local_config, monkeypatch):
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
        assert result["status"] == "rate_limited" and result["ok"] is False
        assert result["data"]["stopped_by"] == "read_quota"
        assert result["data"]["stdout"] == ""
        assert result["data"]["sdk_calls"][0]["execution_started"] is False
        assert not state.metadata["task"]["verified_facts"]
        assert await active.store.due_continuations() == []
    asyncio.run(run())
