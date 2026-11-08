"""脚本接口跨收藏、库存、审计、资源的行为；不访问真实服务或调用模型。"""
from __future__ import annotations

import ast
import asyncio
import doctest
import importlib.util
import inspect
import json
import os
import sqlite3
import subprocess
import sys
import threading
from contextlib import nullcontext
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

import pytest
import requests

from app import database, sdk
from app.clients.guangya import GuangYaFile
from app.indexers.models import (
    AggregatedIndexerResult, IndexerCapabilities, IndexerItem, IndexerProviderError,
    IndexerSitePageState,
)
from tests.support import isolated_test_database


def _qb_response(payload, status=200):
    response = requests.Response()
    response.status_code = status
    response.url = "http://qb.invalid/api/v2/read"
    response._content = json.dumps(payload).encode()
    return response


@pytest.fixture
def recycle_client():
    from tests.test_guangya_read_contracts import ReadClient

    # 复用现有 raw SDK 替身，真实执行 GuangYaClient 的解析、翻页和关闭。
    client = ReadClient(None)
    client._token_lock = threading.RLock()
    rows = [{"fileId": str(i), "fileName": "同名.mkv", "parentId": "source", "resType": 1}
            for i in range(65)]
    client.raw.fs_recycle_files.side_effect = [
        {"data": {"total": 65, "list": rows[:33]}},
        {"data": {"total": 65, "list": rows[32:]}},
    ]
    with patch("app.clients.guangya.GuangYaClient", return_value=client):
        yield client
    client.close()


@pytest.fixture
def qb_session(monkeypatch):
    for key, value in {"QB_URL": "http://qb.invalid", "QB_API_KEY": "private-sdk-key",
                       "QB_USERNAME": "", "QB_PASSWORD": ""}.items():
        monkeypatch.setenv(key, value)
    session = Mock()
    session.get.return_value = _qb_response([
        {"hash": f"{i:064x}" if i == 64 else f"{i:040x}", "name": "同名任务", "state": "checkingUP" if i == 0 else "stoppedUP",
         "progress": 1.0, "category": "tv", "save_path": "/downloads", "content_path": "/downloads/show"}
        for i in range(65)
    ])
    with patch("app.clients.qbittorrent.requests.Session", return_value=session):
        yield session


@pytest.mark.parametrize("enabled", [False, True])
def test_media_resolve_respects_discovery_switch_before_service_creation(monkeypatch, enabled):
    monkeypatch.setenv("DISCOVERY_ENABLED", "1" if enabled else "0")
    service = Mock()
    service.lookup_tmdb_mapping.return_value = {"tmdb_id": "90101", "confirmed": True, "candidates": []}
    with patch("app.discovery.service.get_discovery_service", return_value=service) as factory:
        if enabled:
            result = sdk.media.resolve("douban", "101", "movie", "示例影片", "2026")
            assert result == {"tmdb_id": "90101", "confirmed": True, "candidates": []}
            factory.assert_called_once_with()
            service.lookup_tmdb_mapping.assert_called_once_with("douban", "101", "movie", "示例影片", "2026")
        else:
            with pytest.raises(ValueError, match="影视探索功能当前已关闭"):
                sdk.media.resolve("douban", "101", "movie", "示例影片", "2026")
            factory.assert_not_called()
            service.lookup_tmdb_mapping.assert_not_called()


def test_watchlist_cursor_preserves_all_provider_identities():
    with isolated_test_database():
        for provider, identity, title in (("tmdb", "100", "第一部"), ("tmdb", "200", "第二部"), ("bangumi", "100", "第三部")):
            database.add_media_watchlist(provider, identity, "tv", title, "2026")
        result = sdk.watchlist.list(limit=2)
        identities = list(result["items"])
        assert result["total"] == 3 and result["has_more"]
        result = sdk.watchlist.list(**result["next"])
        identities.extend(result["items"])
        assert not result["has_more"] and result["next"] is None
        assert {(item["provider"], item["external_id"]) for item in identities} == {
            ("tmdb", "100"), ("tmdb", "200"), ("bangumi", "100"),
        }


def test_activity_pages_preserve_ids_for_followup_timeline():
    with isolated_test_database():
        with database.get_conn() as conn:
            conn.executemany(
                "INSERT INTO organize_log(source,original_path,new_path,title,status,created_at,updated_at) "
                "VALUES('guangya','/fixture/source','/fixture/target',?,'failed','2026-01-01','2026-01-02')",
                [(f"活动样本{i}",) for i in range(53)],
            )
        first = sdk.activity.search("活动样本")
        assert first["total"] == 53 and first["returned"] == 50 and first["has_more"]
        last = sdk.activity.search("活动样本", offset=first["next_offset"])
        assert last["returned"] == 3 and not last["has_more"]
        assert len({item["id"] for item in first["items"] + last["items"]}) == 53
        selected = last["items"][-1]
        timeline = sdk.activity.timeline(selected["kind"], selected["id"])
        assert timeline["status"] == "attention"
        assert timeline["data"]["activities"][0] == {"kind": "organize", "id": selected["id"]}
        with database.get_conn() as conn:
            conn.execute("UPDATE organize_log SET status='success' WHERE id=?", (selected["id"],))
        assert sdk.activity.timeline("organize", selected["id"])["status"] == "completed"


def test_native_library_counts_reads_real_scope_without_provider_artifacts(monkeypatch):
    from app import services
    from app.modules.media_server_profiles import MediaServerProfile

    profile = MediaServerProfile("configured:jellyfin", "jellyfin", "Jellyfin",
        "http://media.invalid", "private-count-key", True, "configured-viewer")
    monkeypatch.setattr(services, "list_configured_profiles", lambda: [profile])
    calls = []
    def get(_session, url, **kwargs):
        assert "private-count-key" not in url
        response = requests.Response()
        response.status_code = 200
        if url.endswith("/Library/VirtualFolders"):
            payload = [{"ItemId": "tv", "Name": "电视剧"}]
        else:
            assert url.endswith("/Users/configured-viewer/Items")
            assert kwargs["params"]["ParentId"] == "tv"
            calls.append(kwargs["params"]["IncludeItemTypes"])
            payload = {"TotalRecordCount": 9}
        response._content = json.dumps(payload).encode()
        return response
    monkeypatch.setattr(requests.Session, "get", get)
    result = sdk.METHODS["library.counts"](server="jellyfin", library_id="tv")
    assert "library.counts" not in sdk.WRITE_METHODS
    assert result["status"] == "success" and result["data"]["counts"]["series_count"] == 9
    assert len(calls) == 4 and "private-count-key" not in str(result)
    with pytest.raises(ValueError):
        sdk.library.counts(library_id="tv")
    assert len(calls) == 4


def test_inventory_and_audit_keep_missing_distinct_from_unknown():
    source = {"status": "ready", "server_name": "Jellyfin", "episodes": [(1, 1), (1, 3)], "truncated": True}
    with patch("app.services.inspect_series_episode_sources", return_value=[source]):
        result = sdk.library.episodes("测试剧")
    assert result["data"]["local_episode_count"] == 2
    assert result["data"]["seasons"][0]["episode_ranges"] == [[1, 1], [3, 3]]
    assert result["status"] == "partial" and not result["data"]["inventory_complete"]
    # 校验在共用业务入口进行；脚本正常纠正参数，无需 Agent 错误票据。
    with patch("app.modules.episode_audit.inspect_series_episode_sources", return_value=[]) as inspect_sources:
        with pytest.raises(ValueError, match="season"):
            sdk.library.audit("测试剧", season=-1)
        inspect_sources.assert_not_called()
        result = sdk.library.audit("测试剧", season=1, refresh=True)
        assert not result["ok"] and result["status"] == "not_configured"


def test_batch_presence_reuses_one_inventory_for_more_than_fifty_identities():
    items = [{"tmdb_id": str(i), "media_type": "movie", "title": f"电影{i}"} for i in range(1, 62)]
    with patch("app.services.inspect_media_identity_batch", return_value=[]) as inspect_sources:
        result = sdk.library.presence(items)
    assert result["status"] == "not_configured" and result["data"]["total"] == 61
    inspect_sources.assert_called_once()
    assert len(inspect_sources.call_args.args[0]) == 61


def test_actor_count_does_not_count_the_returned_page_or_lose_movie_constraint():
    from app.services import clear_library_search_cache

    clear_library_search_cache()
    sources = [{"server_type": "jellyfin", "server_name": "Jellyfin", "items": [],
                "total": 149, "has_more": False, "person_match": "exact", "included_item_types": ["Movie"]}]
    try:
        with patch("app.services.search_media_servers", return_value=sources) as search:
            result = sdk.library.search("测试演员", search_by="actor", media_type="movie", count_only=True, limit=1)
        assert result["data"]["total"] == 149
        assert result["data"]["search_by"] == "actor" and result["data"]["media_type"] == "movie"
        assert search.call_args.kwargs["count_only"] is True
        assert search.call_args.kwargs["media_type"] == "movie"
    finally:
        clear_library_search_cache()


def test_library_audit_resumes_with_shared_batch_budget_and_cursor():
    from tests.test_media_library_episode_audit import MediaLibraryEpisodeAuditTests

    fixture = MediaLibraryEpisodeAuditTests()
    client = fixture._tmdb_client()
    with (patch("app.modules.episode_audit.inspect_library_series_sources", return_value=fixture._sources()),
          patch("app.modules.episode_audit.TMDBClient", return_value=client),
          patch("app.modules.episode_audit._MAX_TMDB_REQUESTS", 2)):
        first = sdk.library.audit_all(as_of="2026-08-03")
    assert first["data"]["continuation_pending"]
    assert first["data"]["checked_series_count"] == 1
    assert first["data"]["stalled_tmdb_id"] == "67890"
    client = fixture._tmdb_client()
    with (patch("app.modules.episode_audit.inspect_library_series_sources", return_value=fixture._sources()),
          patch("app.modules.episode_audit.TMDBClient", return_value=client)):
        second = sdk.library.audit_all(as_of="2026-08-03", after_tmdb_id=first["data"]["last_processed_tmdb_id"])
    assert not second["data"]["continuation_pending"]
    assert second["data"]["checked_series_count"] == 1
    client.detail.assert_called_once()
    assert client.detail.call_args.args[0] == "67890"


def test_resource_search_keeps_complete_page_and_error_coverage():
    adapter = SimpleNamespace(site_name="Nyaa", capabilities=IndexerCapabilities(True),
                              http_client_for_result=lambda _: SimpleNamespace(allowed_hosts=("nyaa.si",)))
    registry = SimpleNamespace(ids=lambda: ("nyaa",), enabled_ids=lambda: ("nyaa",), get=lambda _id: adapter)
    items = [IndexerItem("nyaa", "Nyaa", f"Test Show S01E{i:02d} 1080p", result_id=f"candidate_{i:016d}",
                         download_state="ready", download_kinds=("magnet",),
                         detail_url=f"https://{'nyaa.si' if i == 61 else 'untrusted.invalid'}/view/{i}")
             for i in range(1, 62)]

    async def search(request, *, site_ids, scope):
        assert request.media_type == "tv" and scope == "agent" and site_ids == ["nyaa"]
        return AggregatedIndexerResult(
            query=request.title, page=request.page, items=items,
            sites_attempted=("nyaa", "mikan"), sites_succeeded=("nyaa",), has_more=True,
            site_page_states={"nyaa": IndexerSitePageState(True, 1, True, 2)},
            errors=[IndexerProviderError("mikan", "timeout", "token=secret-token-should-not-escape")],
            partial=True, complete=False,
        )

    service = SimpleNamespace(search_media=search, registry=registry, enabled_site_ids=frozenset({"nyaa"}))
    with patch("app.indexers.runtime.get_indexer_service", return_value=service):
        result = sdk.resources.search("Test Show", media_type="tv", season=1, episode=61, sites=["nyaa"])
    assert len(result["items"]) == 61 and result["has_more"]
    assert result["site_statuses"][0]["next_page"] == 2
    assert result["partial"] and not result["complete"]
    assert "secret-token" not in json.dumps(result)
    assert result["recommendation"]["matched_count"] == 1
    assert result["recommendation"]["matched_counts"] == {"exact_episode": 1, "episode_pack": 0}
    assert result["recommendation"]["selected"]["result_id"] == "candidate_0000000000000061"
    assert result["recommendation"]["selected"]["source_url"] == "https://nyaa.si/view/61"
    assert result["recommendation"]["basis"] == "release_title_only"
    assert result["request"]["episode"] == 61
    # 文件里的完整搜索结果能直接重用；重新核对其他集不再请求资源站。
    with patch("app.indexers.runtime.get_indexer_service", side_effect=AssertionError("重复检索")):
        ranked = sdk.resources.rank_episode(json.loads(json.dumps(result)), 1, 60)
    assert ranked["recommendation"]["selected"]["result_id"] == "candidate_0000000000000060"
    assert ranked["recommendation"]["selected"]["source_url"] is None
    assert ranked["recommendation"]["target"] == {"season": 1, "episode": 60}
    assert ranked["recommendation"]["matched_counts"] == {"exact_episode": 1, "episode_pack": 0}
    assert ranked["request"] == json.loads(json.dumps(result["request"]))
    assert ranked["partial"] and not ranked["complete"]


def test_resource_quality_filter_keeps_coverage_and_can_correct_parameters():
    items = [IndexerItem("nyaa", "Nyaa", title, result_id=f"candidate_{i:016d}",
                         download_state="ready", download_kinds=("magnet",)) for i, title in enumerate((
        "Test Show S01E01 1080p SDR", "Test Show S01E01 2160p HDR10",
        "Test Show S01E01 1080p", "Test Show S01E01 1080p HDR10",
        "Test Show S01E01 1080p HLG",
    ))]
    registry = SimpleNamespace(ids=lambda: (), enabled_ids=lambda: ())

    async def search(*args, **kwargs):
        return AggregatedIndexerResult(query="Test Show", page=1, items=items,
                                       sites_attempted=("nyaa", "mikan"), sites_succeeded=("nyaa",),
                                       has_more=True, partial=True, complete=False)

    service = SimpleNamespace(search_media=search, registry=registry)
    with patch("app.indexers.runtime.get_indexer_service", return_value=service) as get_service:
        with pytest.raises(ValueError, match="resolution"):
            sdk.resources.search("Test Show", resolution="FHD")
        get_service.assert_not_called()
        unfiltered = sdk.resources.search("Test Show")
        expected = ["SDR", "HDR10", None, "HDR10", "HLG"]
        for item, dynamic in zip(unfiltered["items"], expected, strict=True):
            assert item["technical_evidence"] == {
                "basis": "release_title_only", "stream_verified": False,
                "dynamic_range": dynamic, "dynamic_ranges": [dynamic] if dynamic else [],
            }
        assert unfiltered["partial"] and unfiltered["has_more"] and not unfiltered["complete"]
        # 旧文件结果即使没有新字段，重排也补同源事实；推荐摘要与完整items一致。
        for item in unfiltered["items"]:
            item.pop("technical_evidence")
        with patch("app.indexers.runtime.get_indexer_service", side_effect=AssertionError("重排不得重搜")):
            ranked = sdk.resources.rank_episode(unfiltered, 1, 1)
        by_id = {item["result_id"]: item for item in ranked["items"]}
        for source, dynamic in zip(items, expected, strict=True):
            assert by_id[source.result_id]["technical_evidence"]["dynamic_range"] == dynamic
        assert by_id[items[0].result_id]["quality"]["score"] == by_id[items[2].result_id]["quality"]["score"]
        assert by_id[items[4].result_id]["quality"]["score"] == by_id[items[2].result_id]["quality"]["score"]
        summaries = [ranked["recommendation"]["selected"], *ranked["recommendation"]["alternatives"]]
        assert summaries and all(summary is not None for summary in summaries)
        for summary in summaries:
            assert summary["technical_evidence"] == by_id[summary["result_id"]]["technical_evidence"]
        assert ranked["partial"] and ranked["has_more"] and not ranked["complete"]
        result = sdk.resources.search("Test Show", resolution="1080p", dynamic_range="SDR")
        assert "recommendation" not in result
        assert [item["result_id"] for item in result["items"]] == [items[0].result_id]
        assert result["partial"] and result["has_more"] and not result["complete"]
        assert result["technical_filter"]["unknown_excluded"] == 1
        assert result["technical_filter"]["mismatched_excluded"] == 3
        assert not result["technical_filter"]["stream_verified"]
        result = sdk.resources.search("Test Show", resolution="1080p", exclude_hdr=True)
        assert [item["result_id"] for item in result["items"]] == [items[0].result_id, items[2].result_id]


def test_resource_request_roundtrip_preserves_scope_and_filters():
    calls = []
    formats = ("1080p SDR", "1080p HDR10", "1080p", "2160p SDR", "2160p HDR10", "2160p")

    async def search(request, *, site_ids, scope):
        calls.append((request, site_ids, scope))
        return AggregatedIndexerResult(query=request.title, page=request.page, items=[
            IndexerItem("nyaa", "Nyaa", f"Demo S{request.season:02d}E01 {fmt}",
                        result_id=f"candidate_{i:016d}", download_state="ready", download_kinds=("magnet",))
            for i, fmt in enumerate(formats)
        ], sites_attempted=("nyaa",), sites_succeeded=("nyaa",))

    service = SimpleNamespace(search_media=search, registry=SimpleNamespace(ids=lambda: (), enabled_ids=lambda: ()))
    expected: dict[str, Any] = {"title": "Demo", "media_type": "tv", "season": 1, "episode": 1, "page": 1,
                "sites": ["nyaa"], "resolution": "1080p", "dynamic_range": "", "exclude_hdr": False}
    with patch("app.indexers.runtime.get_indexer_service", return_value=service):
        result = sdk.resources.search(**expected)
        original = json.loads(json.dumps(result))
        for changes, matching in (
            ({"page": 2}, formats[:3]),
            ({"season": 2}, formats[:3]),
            ({"resolution": "2160p"}, formats[3:]),
            ({"exclude_hdr": True}, ("2160p SDR", "2160p")),
            ({"season": 3}, ("2160p SDR", "2160p")),
            ({"dynamic_range": "SDR"}, ("2160p SDR",)),
            ({"resolution": "480p"}, ()),
            ({"resolution": "1080p"}, ("1080p SDR",)),
            ({"resolution": "", "dynamic_range": "", "exclude_hdr": False, "sites": None}, formats),
        ):
            query = json.loads(json.dumps(result["request"]))
            query.update(changes)
            expected.update(changes)
            result = sdk.resources.search(**query)
            assert {key: result["request"].get(key) for key in expected} == expected
            assert calls[-1][1:] == (expected["sites"], "agent")
            assert calls[-1][0].season == expected["season"]
            assert calls[-1][0].page == expected["page"]
            assert {item["title"] for item in result["items"]} == {
                f"Demo S{expected['season']:02d}E01 {fmt}" for fmt in matching}
            assert result["recommendation"]["matched_count"] == len(matching)
        # 重排不修改冻结的查询，也不再次请求资源站；旧文件同样可读。
        before = len(calls)
        reranked = sdk.resources.rank_episode(original, 1, 2)
        assert reranked["request"] == original["request"]
        assert reranked["request"]["season"] == 1
        assert reranked["recommendation"]["target"] == {"season": 1, "episode": 2}
        for key in ("sites", "resolution", "dynamic_range", "exclude_hdr"):
            original["request"].pop(key)
        sdk.resources.rank_episode(original, 1, 1)
        assert len(calls) == before


@pytest.mark.parametrize("season,episode", [(-1, 2), (1, -3), (True, 2), (None, 2), (1, None), (1, 1001)])
def test_resource_rerank_rejects_invalid_target_without_reporting_empty(season, episode):
    source = {"request": {"title": "Demo", "season": 1, "episode": 1},
              "items": [], "complete": True, "partial": False, "has_more": False}
    before = json.loads(json.dumps(source))
    with patch("app.indexers.runtime.get_indexer_service", side_effect=AssertionError("不得重新搜索")):
        with pytest.raises(ValueError):
            sdk.resources.rank_episode(source, season, episode)
    assert source == before


def test_cloud_reads_keep_provider_identity_and_do_not_hide_failure():
    client = Mock(logged_in=True)
    items = [GuangYaFile(str(i), f"正片{i}.mkv", False, parent_id="series") for i in range(65)]
    root = GuangYaFile("series", "Series", True, parent_id="0")
    by_id = {item.file_id: item for item in [root, *items]}
    client.iter_dir.side_effect = lambda identifier, **kwargs: iter([root] if identifier == "0" else items)
    client.file_info.side_effect = by_id.get
    with patch("app.clients.guangya.GuangYaClient", return_value=client):
        rows = sdk.cloud.list("series")["items"]
        assert len(rows) == 65 and rows[-1]["file_id"] == "64"
        assert sdk.cloud.info(rows[-1]["file_id"])["item"] == asdict(items[-1])
        assert rows[-1]["path"] == "/Series/正片64.mkv"
        assert sdk.cloud.info("missing")["item"] is None
        client.iter_dir.side_effect = RuntimeError("第二页读取失败")
        with pytest.raises(RuntimeError, match="第二页"):
            sdk.cloud.list("series")
        client.logged_in = False
        with pytest.raises(RuntimeError, match="未登录"):
            sdk.cloud.list()
    assert client.close.call_count == 5
    client.delete.assert_not_called()


@pytest.mark.parametrize("stop_at", ["before", "page", "location"])
def test_cloud_listing_cancellation_stops_provider_pages_without_complete_result(stop_at):
    from tests.test_guangya_read_contracts import ReadClient

    client = ReadClient(None)
    client._token_lock = threading.RLock()
    stopped = threading.Event()
    pages = []
    if stop_at == "before":
        stopped.set()

    def page(**arguments):
        pages.append((arguments["parent_id"], arguments["page"]))
        if arguments["parent_id"] is None:
            stopped.set()
            return {"data": {"total": 2, "list": [{"fileId": f"dir-{arguments['page']}",
                    "fileName": "目录", "resType": 2}]}}
        return {"data": {"total": 0, "list": []}}

    def detail(file_id):
        stopped.set()
        return {"data": {"fileId": "source", "fileName": "目录", "resType": 2, "parentId": "0"}}

    client.raw.fs_files.side_effect = page
    client.raw.fs_detail.side_effect = detail
    raw = client.raw
    with patch("app.clients.guangya.GuangYaClient", return_value=client), sdk.call_owner("reader", cancelled=stopped.is_set):
        with pytest.raises(InterruptedError, match="停止"):
            sdk.cloud.list("source" if stop_at == "location" else "0", recursive=True)
    assert pages == ([(None, 0)] if stop_at == "page" else [])
    assert raw.fs_detail.call_count == (1 if stop_at == "location" else 0)


def test_recycle_preserves_complete_pages_and_distinct_provider_ids(recycle_client):
    raw = recycle_client.raw
    result = sdk.cloud.recycle()
    assert [item["file_id"] for item in result["items"]] == [str(i) for i in range(65)]
    assert all(item["parent_id"] == "source" and item["name"] == "同名.mkv" for item in result["items"])
    assert [call.kwargs["page"] for call in raw.fs_recycle_files.call_args_list] == [0, 1]
    raw.close.assert_called_once_with()
    assert recycle_client._raw is None
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("operation", ["restore", "clear_recycle"])
def test_recycle_writes_use_shared_service_real_ids_and_bound_owner(operation):
    from tests.test_agent_guangya_recycle_generation import _RecycleClient

    client = _RecycleClient(7)
    method = getattr(sdk.cloud, operation)
    arguments = {"file_ids": ["trash-1", "trash-1"]} if operation == "restore" else {}
    with patch("app.clients.guangya.GuangYaClient", return_value=client):
        with pytest.raises(ValueError):
            method(**arguments)
        assert client.writes == []
        with sdk.call_owner("native-recycle-test-owner"):
            result = method(**arguments)
    assert result["status"] == "accepted" and not result["verified"]
    assert result["task_id"] == ("restore-task" if operation == "restore" else "clear-task")
    assert len(client.writes) == 1 and client.items == [] and client.closed


def test_cloud_tasks_preserve_complete_list_and_provider_receipt(recycle_client):
    raw = recycle_client.raw
    rows = [{"taskId": str(i), "name": "同名任务", "status": 2 if i == 64 else 1,
             "progress": 0.25, "parentId": "downloads"} for i in range(65)]
    raw.cloud_task_list.side_effect = [{"data": {"total": 65, "list": rows[:50]}},
                                       {"data": {"total": 65, "list": rows[50:]}}]
    result = sdk.cloud.offline_tasks()
    assert [item["id"] for item in result["items"]] == [str(i) for i in range(65)]
    assert result["items"][0]["progress"] == 0.25
    assert result["items"][-1]["status_kind"] == "done"
    raw.close.assert_called_once_with()


def test_cloud_background_task_keeps_running_receipt(recycle_client):
    raw = recycle_client.raw
    raw.get_task_status.return_value = {"data": {"taskId": "copy-1", "status": "running"}}
    receipt = sdk.cloud.task("copy-1")
    assert receipt["data"]["status"] == "running"
    raw.get_task_status.assert_called_once_with("copy-1")
    raw.close.assert_called_once_with()


def test_cloud_operation_uses_bound_identity_and_durable_receipt():
    from app.modules.organize_tasks import OrganizeTaskManager
    from app.repositories.organize_operation_jobs import (
        enqueue_organize_operation_job, organize_operation_public_ref,
    )

    with isolated_test_database():
        task, _ = enqueue_organize_operation_job(
            job_kind="agent_directory_scrape", owner="owner-one", operation="目录刮削",
            reference="fixture", payload={"version": 1}, dedupe_key="sdk-operation",
        )
        reference = organize_operation_public_ref(task["job_id"])
        manager = OrganizeTaskManager()
        with patch("app.modules.organize_tasks.get_organize_manager", return_value=manager), \
                patch.object(manager, "status", return_value={}):
            with pytest.raises(ValueError, match="身份"):
                sdk.cloud.operation(reference)
            with sdk.call_owner("owner-one"):
                result = sdk.cloud.operation(reference)
                assert result["data"]["found"] and result["status"] == "queued"
                assert result["data"]["task"]["operation_ref"] == reference
                with sdk.call_owner("owner-two"):
                    assert not sdk.cloud.operation(reference)["data"]["found"]
                assert sdk.cloud.operation(reference)["data"]["found"]
                database.purge_agent_subject_data(owner="owner-one")
                assert not sdk.cloud.operation(reference)["data"]["found"]
            with pytest.raises(ValueError, match="身份"):
                sdk.cloud.operation(reference)


def test_web_sdk_keeps_article_body_beyond_legacy_preview_and_honors_explicit_budget(monkeypatch):
    from app.clients.http import FixedHostHttpClient, HttpResponse

    monkeypatch.setenv("WEB_READ_ENABLED", "1")
    body = "正文段落。" * 8_000 + "最后一段完整证据"
    url = "https://example.com/sdk-article"

    async def get(_self, requested, **_kwargs):
        assert requested == url
        return HttpResponse(url, 200, {"content-type": "text/plain; charset=utf-8"}, body.encode())

    with isolated_test_database(), patch.object(FixedHostHttpClient, "get", get):
        complete = sdk.web.read(url)
        limited = sdk.web.read(url, max_chars=2_000)
    assert complete["ok"] and complete["data"]["content"] == body
    assert not complete["data"]["truncated"]
    assert complete["data"]["credits_used"] == 0
    assert limited["data"]["truncated"] and len(limited["data"]["content"]) == 2_000
    assert limited["data"]["original_content_chars"] == len(body)


@pytest.mark.parametrize("case", ["empty", "budget", "second_page_error"])
def test_recycle_empty_is_distinct_from_budget_or_page_failure(recycle_client, case):
    raw = recycle_client.raw
    if case == "empty":
        raw.fs_recycle_files.side_effect = None
        raw.fs_recycle_files.return_value = {"data": {"total": 0, "list": []}}
        assert sdk.cloud.recycle() == {"items": []}
    else:
        if case == "second_page_error":
            first = next(raw.fs_recycle_files.side_effect)
            raw.fs_recycle_files.side_effect = [first, {"data": {"list": [None]}}]
        with pytest.raises(RuntimeError):
            sdk.cloud.recycle(max_items=64 if case == "budget" else 20_000)
    raw.close.assert_called_once_with()
    assert recycle_client._raw is None


def test_download_tasks_keep_all_hashes_and_native_state(qb_session):
    result = sdk.downloads.tasks(category="tv")
    assert [item["hash"] for item in result["items"]] == [f"{i:064x}" if i == 64 else f"{i:040x}" for i in range(65)]
    assert result["items"][0]["state"] == "checkingUP" and result["items"][0]["progress"] == 1.0
    assert result["items"][-1]["state"] == "stoppedUP"
    assert qb_session.get.call_args.args == ("http://qb.invalid/api/v2/torrents/info",)
    assert qb_session.get.call_args.kwargs["params"] == {"category": "tv"}
    assert qb_session.get.call_args.kwargs["headers"]["Authorization"] == "Bearer private-sdk-key"
    assert "private-sdk-key" not in json.dumps(result, allow_nan=False)
    qb_session.post.assert_not_called()
    qb_session.close.assert_called_once_with()


@pytest.mark.parametrize("method,args", [("tasks", ()), ("files", ("a" * 40,))])
@pytest.mark.parametrize("payload,status", [([], 200), ({"error": "invalid list"}, 200), ({}, 503)])
def test_download_tasks_empty_and_failures_remain_distinct(qb_session, payload, status, method, args):
    qb_session.get.return_value = _qb_response(payload, status)
    if payload == []:
        assert getattr(sdk.downloads, method)(*args) == {"items": []}
    else:
        with pytest.raises(requests.HTTPError if status == 503 else ValueError):
            getattr(sdk.downloads, method)(*args)
    qb_session.close.assert_called_once_with()


@pytest.mark.parametrize("length", [40, 64])
def test_download_files_keep_full_manifest_and_real_hash(qb_session, length):
    files = [{"index": i, "name": f"Season 01/E{i:03}.mkv", "size": 1000 + i, "progress": 0.5} for i in range(201)]
    qb_session.get.return_value = _qb_response(files)
    assert sdk.downloads.files(" " + "A" * length + " ") == {"items": files}
    assert qb_session.get.call_args.kwargs["params"] == {"hash": "a" * length}
    qb_session.close.assert_called_once()
    qb_session.get.reset_mock()
    for invalid in ("a" * 39, "g" * 40, "all", "a" * 40 + "|" + "b" * 40, None):
        with pytest.raises(ValueError):
            sdk.METHODS["downloads.files"](invalid)
    qb_session.get.assert_not_called()


@pytest.mark.parametrize("authentication", ["key", "password", "denied"])
def test_download_version_authentication_and_cleanup(qb_session, monkeypatch, authentication):
    versions = []
    for text in ("5.2.0\n", "2.12.0\n"):
        response = requests.Response()
        response.status_code = 200
        response._content = text.encode()
        versions.append(response)
    qb_session.get.side_effect = versions
    if authentication != "key":
        monkeypatch.setenv("QB_API_KEY", "")
        monkeypatch.setenv("QB_USERNAME", "viewer")
        monkeypatch.setenv("QB_PASSWORD", "private-password")
        qb_session.post.return_value = Mock(text="Ok." if authentication == "password" else "Fails.")
    if authentication == "denied":
        with pytest.raises(RuntimeError, match="鉴权失败"):
            sdk.downloads.version()
        qb_session.get.assert_not_called()
    else:
        assert sdk.downloads.version() == {"app": "5.2.0", "webapi": "2.12.0"}
        assert [call.args[0] for call in qb_session.get.call_args_list] == [
            "http://qb.invalid/api/v2/app/version", "http://qb.invalid/api/v2/app/webapiVersion"]
    if authentication == "key":
        qb_session.post.assert_not_called()
    else:
        qb_session.post.assert_called_once()
    qb_session.close.assert_called_once()


def test_download_transfer_keeps_native_metrics_and_closes_on_error(qb_session):
    metrics = {"connection_status": "disconnected", "dl_info_speed": 123, "dl_info_data": 456,
               "up_info_speed": 7, "up_info_data": 89, "dl_rate_limit": 1000, "up_rate_limit": 100, "dht_nodes": 4}
    qb_session.get.return_value = _qb_response(metrics)
    qb_session.close.side_effect = RuntimeError("cleanup failed")
    assert sdk.downloads.transfer() == metrics
    assert qb_session.get.call_args.args == ("http://qb.invalid/api/v2/transfer/info",)
    qb_session.get.return_value = _qb_response({}, 503)
    with pytest.raises(requests.HTTPError):
        sdk.downloads.transfer()
    assert qb_session.close.call_count == 2


@pytest.mark.parametrize("queue_failed", [False, True])
@pytest.mark.parametrize("payload", [{}, {"error": "transfer unavailable"}, {"connection_status": None}])
def test_download_diagnosis_rejects_invalid_transfer_facts(qb_session, queue_failed, payload):
    qb_session.get.side_effect = [
        _qb_response([], 503 if queue_failed else 200),
        _qb_response(payload),
    ]
    with isolated_test_database():
        result = sdk.downloads.diagnose()

    assert not result["ok"]
    assert result["status"] == ("unavailable" if queue_failed else "partial")
    assert result["partial"] == (not queue_failed)
    data = result["data"]
    assert data["queue_state"] == ("unavailable" if queue_failed else "reachable")
    if queue_failed:
        assert data["summary"] == {}
    else:
        assert data["summary"]["total"] == 0
    assert data["connection"] == {"api_state": "unavailable"}
    assert data["unavailable_sources"] == (["queue", "transfer"] if queue_failed else ["transfer"])
    assert [call.args[0] for call in qb_session.get.call_args_list] == [
        "http://qb.invalid/api/v2/torrents/info", "http://qb.invalid/api/v2/transfer/info",
    ]
    qb_session.close.assert_called_once_with()
    qb_session.post.assert_not_called()


@pytest.mark.parametrize("method,args", [("tasks", ()), ("transfer", ()), ("version", ()), ("files", ("a" * 40,))])
def test_download_reads_reject_missing_configuration_before_opening_client(monkeypatch, method, args):
    monkeypatch.setenv("QB_URL", "")
    with patch("app.clients.qbittorrent.QBittorrentClient") as factory:
        with pytest.raises(RuntimeError, match="未配置"):
            getattr(sdk.downloads, method)(*args)
    factory.assert_not_called()


def test_metadata_status_reads_real_queue_and_does_not_default_query_failure_to_zero(monkeypatch):
    from app.modules.strm_metadata_worker import get_strm_metadata_worker
    from tests.test_strm_metadata_queue import _job

    monkeypatch.setenv("STRM_METADATA_ENABLED", "0")
    with isolated_test_database():
        empty = sdk.strm.metadata_status()
        assert empty["total"] == 0 and empty["state"] == "paused"
        database.enqueue_strm_metadata_jobs([_job(file_id=str(i)) for i in range(65)])
        with patch.object(get_strm_metadata_worker(), "start", side_effect=AssertionError("只读不得启动工作线程")):
            result = sdk.strm.metadata_status()
        assert result["queued"] == result["pending"] == result["total"] == 65
        assert result["state"] == "paused" and result["worker_scope"] == "current_process"
        assert result["cancellable"] == 65 and result["sampled_at"]
        json.dumps(result, allow_nan=False)
        with patch.object(database, "count_strm_metadata_jobs", side_effect=sqlite3.OperationalError("query failed")):
            with pytest.raises(sqlite3.OperationalError, match="query failed"):
                sdk.strm.metadata_status()


def test_read_methods_use_real_sdk_file_dispatch_and_business_services(tmp_path, monkeypatch, recycle_client, qb_session):
    from app.agent.kernel.sandbox import _SDKRequests
    from app.agent.kernel.capabilities import ToolCatalog
    from app.agent.kernel.effects import EffectExecutor
    from app.agent.kernel.state import InMemorySessionStateStore
    from tests.test_agent_code_sandbox import make_context
    from tests.test_strm_metadata_queue import _job

    monkeypatch.setenv("STRM_METADATA_ENABLED", "0")
    qb_session.get.side_effect = [qb_session.get.return_value, _qb_response({"connection_status": "connected", "dl_info_speed": 321}),
                                  _qb_response({}, 503)]
    (tmp_path / "run").mkdir()
    calls = [("cloud.recycle", {}), ("downloads.tasks", {"category": "tv"}),
             ("downloads.transfer", {}), ("strm.metadata_status", {}), ("downloads.tasks", {}),
             ("downloads.requests", {"scope": "recent", "limit": 1})]

    async def dispatch():
        store = InMemorySessionStateStore()
        context = await make_context(store, owner="sdk-test", session="read-contract")
        rpc = _SDKRequests(context, tmp_path, EffectExecutor(catalog=ToolCatalog(), state_store=store))
        rpc.uid = os.getuid()
        replies = []
        try:
            for index, (method, arguments) in enumerate(calls):
                name = f"{index:032x}.json"
                (tmp_path / "run/mf-requests" / name).write_text(json.dumps({"method": method, "arguments": arguments}))
                await rpc.dispatch(name)
                replies.append(json.loads((tmp_path / "run/mf-responses" / name).read_text()))
            return replies, rpc.calls
        finally:
            await rpc.close()

    with isolated_test_database():
        database.enqueue_strm_metadata_jobs([_job()])
        database.add_media_watchlist("bangumi", "100", "tv", "收藏剧集", "2026")
        row = database.get_media_watchlist("bangumi", "100", "tv")
        assert row is not None
        number = int(row["id"])
        calls.extend([
            ("watchlist.list", {"limit": 1}), ("watchlist.statistics", {}),
            ("watchlist.get", {"watchlist_id": "not-an-integer"}),
            ("watchlist.get", {"watchlist_id": number}),
        ])
        replies, recorded = asyncio.run(dispatch())
    assert len(replies[0]["result"]["items"]) == 65
    assert replies[0]["result"]["items"][-1]["file_id"] == "64"
    assert len(replies[1]["result"]["items"]) == 65
    assert replies[1]["result"]["items"][0]["state"] == "checkingUP"
    assert replies[2]["result"]["dl_info_speed"] == 321
    assert replies[3]["result"]["queued"] == 1 and replies[3]["result"]["worker_scope"] == "current_process"
    assert replies[4]["error"]["code"] == "HTTPError" and "result" not in replies[4]
    assert replies[5]["result"]["items"] == [] and replies[5]["result"]["next_offset"] is None
    assert replies[5]["result"]["scope"] == "recent"
    assert replies[6]["result"]["items"][0]["id"] == number
    assert replies[6]["result"]["total"] == 1 and not replies[6]["result"]["has_more"]
    assert replies[7]["result"]["total"] == 1
    assert replies[7]["result"]["by_media_type"][0]["latest"][0]["id"] == number
    assert replies[8]["error"]["code"] == "ValueError" and "result" not in replies[8]
    assert replies[9]["result"]["item"]["provider"] == "bangumi"
    assert replies[9]["result"]["item"]["external_id"] == "100"
    assert replies[9]["result"]["item"]["id"] == number
    assert [item["method"] for item in recorded] == [method for method, _ in calls]
    assert [item["ok"] for item in recorded] == [True, True, True, True, False, True, True, True, False, True]
    assert qb_session.close.call_count == 3 and recycle_client._raw is None


@pytest.fixture
def sdk_client(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "modules", sys.modules.copy())
    client = Path(__file__).parents[1] / "app/agent/kernel/workspace_client.py"
    (tmp_path / "mf.py").write_text(client.read_text())
    (tmp_path / "mf-methods.json").write_text(json.dumps(sdk.describe_methods()))
    spec = importlib.util.spec_from_file_location("sdk_client_probe", tmp_path / "mf.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []
    def request(name, arguments):
        values = {key: value for key, value in arguments.items() if value is not Ellipsis}
        calls.append((name, values))
        return values
    for function in module._METHODS.values():
        function.__globals__["_request"] = request
    return module, calls


def test_sdk_registration_requires_an_explicit_effect_without_wrapping_functions():
    class Undeclared:
        @staticmethod
        def remove():
            raise AssertionError("registration must not execute methods")

    with pytest.raises(ValueError, match="必须声明"):
        sdk._collect_methods({"new": Undeclared()})

    Undeclared.remove = staticmethod(sdk.sdk_method("write")(Undeclared.remove))
    methods, effects = sdk._collect_methods({"new": Undeclared()})
    assert methods["new.remove"] is Undeclared.remove
    assert effects == {"new.remove": "write"}
    assert set(sdk.METHOD_EFFECTS) == set(sdk.METHODS)
    assert sdk.WRITE_METHODS == {name for name, effect in sdk.METHOD_EFFECTS.items() if effect == "write"}


def test_python_client_and_cli_share_real_signatures(tmp_path, sdk_client):
    module, calls = sdk_client
    assert "本地媒体库" in module.library.__doc__
    assert "公开资源站" in module.resources.__doc__
    assert "resources" in dir(module) and "os" not in dir(module)
    # 普通Python内省必须能读到对应方法的代理声明与文档，而非共享的匿名代码块。
    for name in ("library.audit", "media.calendar", "cloud.list"):
        function = module._METHODS[name]
        source = inspect.getsource(function)
        assert f"def {name.split('.')[1]}(" in source and name in source
        declaration = ast.parse(source).body[0]
        assert isinstance(declaration, ast.FunctionDef)
        assert ast.get_docstring(declaration) == inspect.getdoc(function)
    for name, method in sdk.METHODS.items():
        assert inspect.signature(module._METHODS[name]) == inspect.signature(method, eval_str=True)
        assert inspect.getdoc(module._METHODS[name]) == inspect.getdoc(method)
    # 已生成的方法仍走同一个请求函数，保留默认参数及正常 Python 参数错误。
    result = module.library.audit("测试剧", tmdb_id="123", refresh=True)
    assert result["query"] == "测试剧" and result["refresh"] is True
    with pytest.raises(TypeError):
        module.library.audit("测试剧", nonexisting=True)
    assert len(calls) == 1
    completed = subprocess.run([sys.executable, "-m", "mf", "library.audit", "--help"],
                               cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stderr
    assert "--tmdb-id" in completed.stdout and "--refresh" in completed.stdout
    assert "--no-refresh" in completed.stdout and "--query" in completed.stdout
    overview = subprocess.run([sys.executable, "-m", "mf", "--help"],
                              cwd=tmp_path, capture_output=True, text=True, timeout=10)
    detailed = subprocess.run([sys.executable, "-m", "mf", "resources.search", "--help"],
                              cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert overview.returncode == detailed.returncode == 0
    assert len(overview.stdout) < 4096  # 总览不能挤满命令输出窗口，后续参数帮助必须可见。
    for namespace in sdk.describe_namespaces():
        assert namespace in overview.stdout
    section = subprocess.run([sys.executable, "-m", "mf", "library", "--help"],
                             cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert section.returncode == 0
    assert all(name in section.stdout for name in sdk.METHODS if name.startswith("library."))
    assert "cloud.recycle" not in section.stdout
    assert "matched_count" in detailed.stdout and "recommendation" in detailed.stdout
    assert "matched_count" not in overview.stdout  # 总览只展示简介，完整契约按命令查看。
    imported = subprocess.run([sys.executable, "-c",
        "from mf.resources import search; import inspect; print(inspect.signature(search))"],
        cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert imported.returncode == 0, imported.stderr
    assert "title: str" in imported.stdout
    assert {"cloud.recycle", "downloads.submit", "downloads.tasks", "downloads.transfer", "strm.metadata_status"} <= module._METHODS.keys()
    assert {"system.automation_summary", "organize.status"}.isdisjoint(module._METHODS)
    submitted = module.downloads.submit("magnet:?xt=urn:btih:" + "a" * 40)
    assert submitted["target"] == "preferred"
    assert calls[-1] == ("downloads.submit", submitted)
    result = module.cloud.recycle(max_items=100_000)
    assert result == {"max_items": 100_000} and calls[-1][0] == "cloud.recycle"
    with patch.object(sys, "argv", ["mf", "downloads.tasks", "--category", "tv"]):
        module.main()
    assert calls[-1] == ("downloads.tasks", {"category": "tv"})
    identities = [{"tmdb_id": "123", "media_type": "movie", "title": "同名电影"}]
    (tmp_path / "identities.json").write_text(json.dumps(identities))
    with patch.object(sys, "argv", ["mf", "library.presence", "--items", "@" + str(tmp_path / "identities.json")]):
        module.main()
    assert calls[-1] == ("library.presence", {"items": identities})


def test_sdk_help_examples_read_real_response_paths(sdk_client):
    from app.clients.base import SeriesEpisodeInventory
    from app.discovery.models import MediaCard
    from app.modules.episode_audit import reset_episode_audit_cache_for_tests
    from tests.test_media_episode_audit import _FakeTMDB, _ready

    module, _ = sdk_client
    inventory = SeriesEpisodeInventory(episodes=[(1, 1), (1, 3)], total=2)
    client = Mock(list_series_episode_inventory=Mock(return_value=inventory))
    card = MediaCard(provider="tmdb", media_type="tv", external_id="12345", title="目标剧",
                     seasons=({"season_number": 1, "episode_count": 4},))
    discovery = Mock(describe_media=Mock(return_value=card.to_dict()))
    methods = ("watchlist.list", "watchlist.get", "library.episodes", "library.audit", "library.updates", "media.detail")
    reset_episode_audit_cache_for_tests()
    try:
        with (isolated_test_database(),
              patch("app.services.inspect_series_episode_sources", return_value=[_ready(inventory.episodes)]),
              patch("app.modules.episode_audit.inspect_series_episode_sources", return_value=[_ready(inventory.episodes)]),
              patch("app.modules.episode_audit.TMDBClient", side_effect=_FakeTMDB),
              patch("app.modules.media_library._client", return_value=nullcontext((SimpleNamespace(server_type="jellyfin"), client))),
              patch("app.discovery.service.require_discovery_enabled"),
              patch("app.discovery.service.get_discovery_service", return_value=discovery)):
            database.add_media_watchlist("tmdb", "12345", "tv", "目标剧", "2026")
            runner = doctest.DocTestRunner()
            for name in methods:
                function = module._METHODS[name]
                function.__globals__["_request"] = lambda method, arguments: sdk.METHODS[method](**arguments)
                example = doctest.DocTestParser().get_doctest(inspect.getdoc(function) or "",
                    {"library": module.library, "watchlist": module.watchlist, "media": module.media}, name, None, 0)
                assert example.examples
                assert runner.run(example).failed == 0
    finally:
        reset_episode_audit_cache_for_tests()


@pytest.mark.parametrize("encoding", ["text", "json", "file", "empty_array"])
def test_cli_string_collection_union_preserves_text_and_json(sdk_client, tmp_path, encoding):
    module, calls = sdk_client
    urls = ["https://example.invalid/a?x=1", "https://example.invalid/b"]
    expected = "\n".join(urls) if encoding == "text" else [] if encoding == "empty_array" else urls
    value = expected if isinstance(expected, str) else json.dumps(expected)
    if encoding == "file":
        path = tmp_path / "urls.json"
        path.write_text(value)
        value = "@" + str(path)
    with patch.object(sys, "argv", ["mf", "rss.update", "--subscription-id", "17", "--urls", value]):
        module.main()
    assert calls == [("rss.update", {"subscription_id": 17, "urls": expected})]
    assert module.rss.update(17, urls=expected) == calls[0][1]


def test_cli_malformed_union_json_stops_before_request(sdk_client):
    module, calls = sdk_client
    with patch.object(sys, "argv", ["mf", "rss.update", "--subscription-id", "17", "--urls", '["unfinished"']), pytest.raises(SystemExit) as error:
        module.main()
    assert error.value.code == 2
    assert calls == []


@pytest.mark.parametrize("encoding", ["arguments", "file", "empty_arguments", "empty_file"])
def test_cli_string_lists_preserve_batch_file_values(sdk_client, tmp_path, encoding):
    module, calls = sdk_client
    values = [] if encoding.startswith("empty") else [f"候选 {index} [版本]" for index in range(65)]
    arguments = values
    if encoding.endswith("file"):
        path = tmp_path / "候选 IDs.json"
        path.write_text(json.dumps(values, ensure_ascii=False))
        arguments = ["@" + str(path)]
    with patch.object(sys, "argv", ["mf", "resources.present", "--result-ids", *arguments]):
        module.main()
    assert calls == [("resources.present", {"result_ids": values})]


@pytest.mark.parametrize("content", ['{"ids": ["one"]}', '["one", 2]', 'null', '["unfinished"', None])
def test_cli_invalid_string_list_file_stops_before_request(sdk_client, tmp_path, content):
    module, calls = sdk_client
    path = tmp_path / "ids.json"
    if content is not None:
        path.write_text(content)
    with patch.object(sys, "argv", ["mf", "resources.download", "--result-ids", "@" + str(path)]), pytest.raises(SystemExit) as error:
        module.main()
    assert error.value.code == 2
    assert calls == []


@pytest.mark.parametrize("method,base,field,default", [
    ("resources.search", ["--title", "Example"], "sites", None),
    ("preferences.update", [], "preferred_codecs", Ellipsis),
])
@pytest.mark.parametrize("encoding", ["omitted", "empty_arguments", "empty_file"])
def test_cli_optional_string_lists_preserve_omitted_and_empty(sdk_client, tmp_path, method, base, field, default, encoding):
    module, calls = sdk_client
    arguments = [] if encoding == "omitted" else ["--" + field.replace("_", "-")]
    if encoding == "empty_file":
        path = tmp_path / "empty.json"
        path.write_text("[]")
        arguments.append("@" + str(path))
    with patch.object(sys, "argv", ["mf", method, *base, *arguments]):
        module.main()
    assert calls[0][1].get(field, Ellipsis) == (default if encoding == "omitted" else [])


@pytest.mark.parametrize("arguments", [
    {"title": ""}, {"title": "Demo", "aliases": ["x"] * 9},
    {"title": "Demo", "year": True}, {"title": "Demo", "page": 0},
    {"title": "Demo", "season": True}, {"title": "Demo", "season": -1},
    {"title": "Demo", "season": 101}, {"title": "Demo", "episode": 0},
    {"title": "Demo", "episode": 1001}, {"title": "Demo", "sort_mode": "newest"},
])
def test_resource_search_uses_shared_request_validation_before_query(arguments):
    with patch("app.indexers.runtime.get_indexer_service") as service:
        with pytest.raises(ValueError):
            sdk.resources.search(**arguments)
        service.assert_not_called()


def test_resource_search_preserves_shared_title_normalization():
    from tests.test_agent_indexer_actions import FakeIndexerService

    service = FakeIndexerService(partial=False)
    with patch("app.indexers.runtime.get_indexer_service", return_value=service):
        result = sdk.resources.search(" \tDemo\n")
    assert service.search_calls[0][0].title == result["request"]["title"] == result["query"] == "Demo"


def test_native_download_url_sdk_uses_common_service_and_host_default_owner():
    with isolated_test_database(), sdk.call_owner("url-sdk-owner"):
        sdk.preferences.update(preferred_download_target="qb")
        with patch("app.indexers.downloads.submit_download_url", return_value={"request_id": 42}) as submit:
            assert sdk.downloads.submit("magnet:?xt=urn:btih:" + "a" * 40) == {"request_id": 42}
        submit.assert_called_once_with("magnet:?xt=urn:btih:" + "a" * 40, "qb", origin="agent")
        assert "downloads.submit" in sdk.WRITE_METHODS
    with patch("app.indexers.downloads.submit_download_url") as submit:
        with pytest.raises(ValueError, match="身份"):
            sdk.downloads.submit("magnet:?xt=urn:btih:" + "a" * 40, "qb")
        submit.assert_not_called()


def test_cloud_media_inspect_search_and_scrape_use_shared_service_real_ids(tmp_path):
    from app.agent.models import ToolContext
    from app.repositories.organize_operation_jobs import get_organize_operation_job_for_owner, organize_operation_job_id_from_public_ref
    from tests.test_agent_effect_admission import _OfflineClient, _offline_runtime, _paused_manager, _domain_arguments

    client = _OfflineClient()
    with isolated_test_database(), _offline_runtime(tmp_path, client) as stack, _paused_manager(), sdk.call_owner("sdk-media-owner"):
        arguments = _domain_arguments("scrape", client, ToolContext(owner="sdk-media-owner"), stack)
        file_id = arguments["file_id"]
        assert isinstance(file_id, str)
        inspected = sdk.cloud.inspect_media(file_id)
        assert [row["file_id"] for row in inspected["videos"]] == ["rename"]
        searched = sdk.cloud.search_media(file_id, "Iron Man", "movie", "2008")
        candidate = searched["candidates"][0]
        assert (candidate["provider"], candidate["external_id"], candidate["tmdb_id"]) == ("tmdb", "1726", "1726")
        first = sdk.cloud.scrape(file_id, candidate["media_type"], provider=candidate["provider"],
            external_id=candidate["external_id"], tmdb_id=candidate["tmdb_id"])
        assert first["status"] == "accepted"
        job_id = organize_operation_job_id_from_public_ref(first["data"]["operation_ref"])
        row = get_organize_operation_job_for_owner(job_id, "sdk-media-owner")
        assert row is not None and row["status"] == "pending"
        payload = json.loads(row["payload_json"])
        assert payload["version"] == 2 and payload["flow"]["selection_source"] == "automatic"
        assert get_organize_operation_job_for_owner(job_id, "foreign-owner") is None
        second = sdk.cloud.scrape(file_id, "movie", tmdb_id="1726")
        assert second["data"]["replayed"]
        assert second["data"]["operation_ref"] == first["data"]["operation_ref"]
        file = client.file_info("rename")
        assert file is not None and file.parent_id == "source"
