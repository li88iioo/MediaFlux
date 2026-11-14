"""全链路复核：刷新未定位不能丢失持久意图，路径压缩不能二次方放大。"""
from __future__ import annotations

import time
from unittest.mock import Mock, patch

from app.clients.jellyfin import JellyfinClient
from app.clients.emby import EmbyClient
from app.modules import media_refresh
from app.modules.media_refresh_coordinator import MediaRefreshCoordinator
from app.repositories.media_refresh_queue import (
    claim_due_media_refreshes,
    enqueue_media_refresh,
    media_refresh_queue_status,
)
from tests.support import isolated_test_database


def test_real_unmapped_refresh_result_is_retained_not_acknowledged():
    for client_type, provider in ((JellyfinClient, "jellyfin"), (EmbyClient, "emby")):
        with isolated_test_database():
            enqueue_media_refresh(provider, ["/not-mapped/Ultraman"], debounce_seconds=0, now_epoch=100)
            job = claim_due_media_refreshes(owner="audit", now_epoch=100)[0]
            client = client_type("http://media.invalid", "test-key")
            client.list_virtual_folders = lambda: [{"id": "tv", "name": "TV", "locations": ["/media/TV"]}]
            client.refresh_all = Mock(side_effect=AssertionError("global refresh forbidden"))
            coordinator = MediaRefreshCoordinator()
            coordinator._owner = "audit"
            # 真实客户端刷新规划；仅替换远端目录读取，不伪造 ok/retryable DTO。
            with patch.object(coordinator, "_client_for", return_value=client):
                coordinator._process_group(job)
            status = media_refresh_queue_status()
            assert status["paths"] == 1, status
            assert status["retry_wait"] == 1, status
            assert coordinator._completed_session == 0
            client.refresh_all.assert_not_called()


def test_no_target_refresh_is_an_explicit_successful_noop():
    client = JellyfinClient("http://media.invalid", "test-key")
    try:
        result = client.refresh_for_paths([], allow_global_fallback=False)
        assert result["ok"] is True
        assert result["skipped"] is True
    finally:
        client.close()


def test_movie_directory_refresh_settles_only_after_covering_scan_succeeds():
    for client_type, provider in ((JellyfinClient, "jellyfin"), (EmbyClient, "emby")):
        with isolated_test_database():
            plan = media_refresh.plan_refresh_targets(changed_paths=["/media/flat/B.strm"])
            enqueue_media_refresh(provider, list(plan.targets), debounce_seconds=0)
            client = client_type("http://media.invalid", "test-key")
            client.list_virtual_folders = lambda: [{"id": "movies", "locations": ["/media"]}]
            client._library_items_with_paths = lambda library_id: [
                {"Id": "root", "Type": "Folder", "Path": "/media"},
                {"Id": "old", "Type": "Movie", "Path": "/media/flat/Long-existing-title.strm"},
            ]  # B 还未被媒体服务器发现，刷新 old 不能覆盖它。
            refresh = Mock(side_effect=[False, True])
            client.refresh_library = refresh
            client.refresh_all = Mock(side_effect=AssertionError("global refresh forbidden"))
            worker = MediaRefreshCoordinator()
            worker._owner = "directory-audit"
            try:
                with patch.object(worker, "_client_for", return_value=client):
                    worker._process_group(claim_due_media_refreshes(owner=worker._owner)[0])
                    assert media_refresh_queue_status()["paths"] == 1
                    assert media_refresh_queue_status()["retry_wait"] == 1
                    job = claim_due_media_refreshes(owner=worker._owner, now_epoch=time.time() + 3600)[0]
                    worker._process_group(job)
                assert media_refresh_queue_status()["paths"] == 0
                assert [call.args for call in refresh.call_args_list] == [("root",), ("root",)]
                client.refresh_all.assert_not_called()
            finally:
                client.close()


def test_descendant_compression_uses_linear_boundary_checks():
    paths = [f"/media/TV/Series-{i:05d}" for i in range(2000)]
    calls = 0
    original = media_refresh._is_descendant
    def count(candidate, ancestor):
        nonlocal calls
        calls += 1
        return original(candidate, ancestor)
    with patch.object(media_refresh, "_is_descendant", new=count):
        result = media_refresh._drop_descendants(paths)
    assert set(result) == set(paths)
    assert calls <= len(paths) * 2, calls


def test_descendant_compression_handles_separator_adjacent_siblings():
    paths = ["/media/A", "/media/A-b", "/media/A/b", "/media/A_b", "/media/A.b", "/media/A.b/C"]
    result = media_refresh.plan_refresh_targets(changed_dirs=paths, media_roots=["/media"])
    assert result.targets == ("/media/A", "/media/A-b", "/media/A.b", "/media/A_b")


def test_zero_and_negative_batch_size_follow_refresh_plan_contract():
    for size in (0, -1):
        result = media_refresh.plan_refresh_targets(changed_dirs=["/media/A"], max_targets=size)
        assert result.targets == ("/media/A",)
        assert result.batch_size > 0
        assert result.batches == (("/media/A",),)
