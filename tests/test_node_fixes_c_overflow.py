"""C02：真实 scheduler/outbox/消费者，包含默认无前缀来源与停止策略。"""

from typing import Any

from typing import cast
from app.clients.guangya import GuangYaClient
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pytest

from app.repositories import strm as strm_repo
from app.clients.emby import EmbyClient
from app.clients.guangya import GuangYaFile
from app.clients.jellyfin import JellyfinClient
from app.modules import strm
from app.modules.media_refresh import plan_refresh_targets
from app.modules.scheduler import STRMScheduler
from app.modules.strm_metadata_worker import STRMMetadataWorker
from tests.support import isolated_test_database
from tests.test_media_precise_refresh import _RefreshRecorder
from tests.test_strm_hardening import _TreeClient


@pytest.mark.parametrize("mode", ["full", "fast"])
@pytest.mark.parametrize("prefix", ["", "SourceA"])
@pytest.mark.parametrize("refresh_enabled,allow_emby", [(True, True), (True, False), (False, False)])
@pytest.mark.parametrize("stop", [False, True])
def test_C02_saturated_sync_delivers_every_library_with_policy(tmp_path, mode, prefix, refresh_enabled, allow_emby, stop):
    with isolated_test_database(), ExitStack() as stack:
        scheduler = STRMScheduler()
        source = {"id": "source", "name": "source", "rel_prefix": prefix}
        directories = [GuangYaFile(f"Library{i}", f"Library{i}", True) for i in range(6)]
        tree = {"source": directories}
        changes = []
        for i, directory in enumerate(directories):
            media = GuangYaFile(str(i), f"E{i}.mkv", False, 100, "e", directory.file_id)
            tree[directory.file_id] = [media]
            changes.append({"source_id": "source", "kind": "video", "action": "upsert",
                "file_id": str(i), "name": media.name, "rel_dir": directory.name,
                "parent_id": directory.file_id, "etag": "e", "size": 100})
        cloud = _TreeClient(tree)
        values = {"STRM_ROOT": str(tmp_path), "GY_STRM_BASE_URL": "http://play.invalid"}
        for name, value in [("validate_config", ""), ("_source_dirs", [source]),
                            ("_video_exts", {"mkv"}), ("_metadata_exts", set())]:
            stack.enter_context(patch.object(scheduler, name, return_value=value))
        for name in ("_notify_success", "_notify_details"):
            stack.enter_context(patch.object(scheduler, name))
        stack.enter_context(patch("app.modules.scheduler.get", side_effect=lambda k, d="": values.get(k, d)))
        stack.enter_context(patch("app.modules.scheduler.get_int", return_value=0))
        stack.enter_context(patch("app.modules.scheduler.configured_strm_source_plans", return_value=([source], "")))
        stack.enter_context(patch("app.modules.scheduler.sync_strm", side_effect=lambda **kw: strm.sync_strm(client=cast(GuangYaClient, cloud), **kw)))
        stack.enter_context(patch("app.modules.scheduler.sync_strm_incremental", side_effect=lambda **kw: strm.sync_strm_incremental(client=cast(GuangYaClient, cloud), **kw)))
        stack.enter_context(patch.object(strm, "_MAX_TRACKED_CHANGED_PATHS", 1))
        stack.enter_context(patch.object(strm, "_MAX_TRACKED_OVERFLOW_DIRS", 1))
        handoff = stack.enter_context(patch("app.modules.media_refresh_coordinator.enqueue_media_refresh_paths", return_value={"Jellyfin": "failed"}))
        execute = scheduler._execute_locked
        def execute_with_policy(trigger, run_id):
            scheduler._run_options.update(media_server_refresh_override=refresh_enabled, emby_refresh_override=allow_emby)
            return execute(trigger, run_id)
        stack.enter_context(patch.object(scheduler, "_execute_locked", side_effect=execute_with_policy))
        original_track = strm._track_change
        generated = []
        def stop_after_saturation(*args, **kwargs):
            original_track(*args, **kwargs)
            if args[1] == "generated":
                generated.append(args[2])
                if stop and len(generated) == 6:
                    scheduler._stop_event.set()
        stack.enter_context(patch.object(strm, "_track_change", side_effect=stop_after_saturation))
        options = {"sync_mode": mode}
        if mode == "fast":
            options["organize_changes"] = changes
        result = scheduler.run_blocking("manual", **options)
        assert len(generated) == 6, result
        assert bool(result.get("stopped")) is stop
        if stop or not refresh_enabled:
            handoff.assert_not_called()
        old = strm_repo.list_strm_refresh_entries()
        expected = {str(tmp_path / strm.STRM_SUBDIR / prefix / f"Library{i}") for i in range(6)}
        assert {e["path"] for e in old} == (expected if refresh_enabled else set())
        assert all(e["allow_emby"] is allow_emby for e in old)
        assert len(result["stats"]["changed_strm_paths"]) <= 1
        assert len(result["stats"]["changed_overflow_dirs"]) <= 1
        scheduler._stop_event.clear()
        # 先模拟恢复后的队列交接失败；outbox 令牌不变。
        STRMMetadataWorker()._flush_media_refresh(force=True)
        assert strm_repo.list_strm_refresh_entries() == old
        # 然后用真实 Jellyfin/Emby 解析器消费独立库根，禁止全局刷新。
        recorders = {}
        def consume(paths, **kw):
            assert kw["allow_emby"] is allow_emby
            providers = [("Jellyfin", JellyfinClient)] + ([("Emby", EmbyClient)] if kw["allow_emby"] else [])
            for name, cls in providers:
                client = cls("http://media.invalid", "fixture-token")
                folders = [{"id": f"lib{i}", "name": f"Library{i}", "locations": [path]} for i, path in enumerate(sorted(expected))]
                items = {f"lib{i}": [{"Id": f"folder{i}", "Type": "Folder", "Path": path}] for i, path in enumerate(sorted(expected))}
                recorder = _RefreshRecorder(client, folders, items)
                outcome = client.refresh_for_paths(paths, allow_global_fallback=False)
                assert outcome["ok"] and outcome["matched"] == 6, outcome
                assert set(recorder.refreshed) == {f"folder{i}" for i in range(6)}
                assert recorder.refresh_all_calls == 0
                recorders[name] = recorder
                client.close()
            # 交接期间新事件覆盖同路径，旧快照 ACK 不得吞掉新令牌。
            strm_repo.enqueue_strm_refresh_paths([max(expected)], allow_emby=allow_emby)
            return {name: "queued" for name, _ in providers}
        with patch("app.modules.media_refresh_coordinator.enqueue_media_refresh_paths", side_effect=consume):
            STRMMetadataWorker()._flush_media_refresh(force=True)
        assert set(recorders) == (({"Jellyfin", "Emby"} if allow_emby else {"Jellyfin"}) if refresh_enabled else set())
        assert strm_repo.acknowledge_strm_refresh_paths(old) == 0
        assert strm_repo.count_strm_refresh_paths() == int(refresh_enabled)
        with patch("app.modules.media_refresh_coordinator.enqueue_media_refresh_paths", return_value={"Jellyfin": "queued"}):
            STRMMetadataWorker()._flush_media_refresh(force=True)
        assert strm_repo.count_strm_refresh_paths() == 0


def test_C02_unconfirmed_media_root_stays_forbidden():
    root = "/fixture/strm"
    assert not plan_refresh_targets(changed_dirs=[root], media_roots=[root]).has_targets


@pytest.mark.parametrize("allow_emby", [True, False])
def test_C02_exact_spill_survives_real_refresh_queue_and_consumers(tmp_path, allow_emby):
    from app.modules import media_refresh_coordinator as coordinator
    from app.repositories.media_refresh_queue import (
        claim_due_media_refreshes,
        media_refresh_queue_status,
    )

    with isolated_test_database(), ExitStack() as stack:
        scheduler = STRMScheduler()
        scheduler._run_options.update(emby_refresh_override=allow_emby)
        stack.enter_context(patch("app.modules.scheduler.get", side_effect=lambda k, d="": str(tmp_path) if k == "STRM_ROOT" else d))
        stack.enter_context(patch.object(strm, "_MAX_TRACKED_CHANGED_PATHS", 1))
        stack.enter_context(patch.object(strm, "_MAX_TRACKED_OVERFLOW_DIRS", 1))
        stats = {}
        paths = [tmp_path / strm.STRM_SUBDIR / f"Library{i}" / "E.strm" for i in range(6)]
        for path in paths:
            strm._record_changed_path(stats, path, on_refresh_paths=scheduler._refresh_overflow_sink())
        strm.finalize_changed_paths(stats)
        scheduler._refresh_media_servers(changed_paths=stats["changed_strm_paths"],
            changed_dirs=stats["changed_dirs"], emby_enabled=allow_emby, persist_only=True)
        assert strm_repo.count_strm_refresh_paths() == 6
        worker = coordinator.MediaRefreshCoordinator()
        stack.enter_context(patch.object(coordinator, "get_media_refresh_coordinator", return_value=worker))
        stack.enter_context(patch.object(worker, "wake"))
        stack.enter_context(patch.object(coordinator, "_configured_provider_names",
            side_effect=lambda *, allow_emby: ("jellyfin", "emby") if allow_emby else ("jellyfin",)))
        # 不替换 enqueue：outbox -> 统一 SQLite 队列 -> 条件 ACK。
        STRMMetadataWorker()._flush_media_refresh(force=True)
        assert strm_repo.count_strm_refresh_paths() == 0
        groups = claim_due_media_refreshes(owner=worker._owner, force=True)
        assert {g["provider"] for g in groups} == ({"jellyfin", "emby"} if allow_emby else {"jellyfin"})
        for group in groups:
            cls = JellyfinClient if group["provider"] == "jellyfin" else EmbyClient
            client = cls("http://media.invalid", "fixture-token")
            folders = [{"id": f"lib{i}", "name": f"Library{i}", "locations": [str(path.parent)]} for i, path in enumerate(paths)]
            items = {f"lib{i}": [{"Id": f"folder{i}", "Type": "Folder", "Path": str(path.parent)}] for i, path in enumerate(paths)}
            recorder = _RefreshRecorder(client, folders, items)
            with patch.object(worker, "_client_for", return_value=client):
                worker._process_group(group)
            assert set(recorder.refreshed) == {f"folder{i}" for i in range(6)}
            assert recorder.refresh_all_calls == 0
        assert media_refresh_queue_status()["paths"] == 0


@pytest.mark.parametrize("mode", ["full", "fast"])
def test_C02_spill_write_failure_keeps_batch_and_stops_further_install(tmp_path, mode):
    with isolated_test_database(), ExitStack() as stack:
        cloud = _TreeClient({"source": [GuangYaFile(f"L{i}", f"L{i}", True) for i in range(10)],
            **{f"L{i}": [GuangYaFile(str(i), f"E{i}.mkv", False, 100, "e", f"L{i}")] for i in range(10)}})
        stack.enter_context(patch.object(strm, "_MAX_TRACKED_CHANGED_PATHS", 1))
        stack.enter_context(patch.object(strm, "_MAX_TRACKED_OVERFLOW_DIRS", 1))
        def fail(paths):
            raise OSError("temporary SQLite write failure")
        if mode == "full":
            stats = strm.sync_strm("source", "http://play.invalid", str(tmp_path), client=cast(GuangYaClient, cloud),
                on_refresh_paths=fail, clean_invalid=False, clean_empty_dirs=False)
        else:
            changes = [{"source_id": "source", "kind": "video", "action": "upsert", "file_id": str(i),
                        "name": f"E{i}.mkv", "rel_dir": f"L{i}", "parent_id": f"L{i}"} for i in range(10)]
            stats = strm.sync_strm_incremental("source", changes, "http://play.invalid", str(tmp_path),
                client=cast(GuangYaClient, cloud), on_refresh_paths=fail)
        assert stats["stopped"] and stats["stop_stage"] == "refresh-persist"
        assert stats["generated"] == 3
        assert len(stats["changed_overflow_dirs"]) == 2  # 失败批次不删除；下一项前停止
        strm.finalize_changed_paths(stats)
        assert len(stats["changed_dirs"]) == 3
        rows = strm_repo.list_strm_index("guangya:source")
        assert len(rows) == 3
        assert {str(Path(r["strm_path"]).parent) for r in rows} == set(stats["changed_dirs"])


def test_C02_missing_policy_never_implicitly_enqueues_refresh(tmp_path):
    with isolated_test_database(), patch.object(strm, "_MAX_TRACKED_CHANGED_PATHS", 1), patch.object(strm, "_MAX_TRACKED_OVERFLOW_DIRS", 1):
        stats = {}
        for i in range(3):
            strm._record_changed_path(stats, tmp_path / f"Library{i}" / "E.strm")
        assert strm_repo.count_strm_refresh_paths() == 0
        assert stats["stopped"] and stats["stop_stage"] == "refresh-persist"
        strm.finalize_changed_paths(stats)
        assert len(stats["changed_dirs"]) == 3  # 返回全部已发生变化，不猜 provider


@pytest.mark.parametrize("stage", ["full", "fast", "deferred-cleanup"])
@pytest.mark.parametrize("allow_emby", [True, False])
def test_C02_scheduler_retries_failed_spill_on_stop_without_consuming(tmp_path, stage, allow_emby):
    with isolated_test_database(), ExitStack() as stack:
        source = {"id": "source", "name": "source", "rel_prefix": ""}
        tree = {"source": [GuangYaFile(f"L{i}", f"L{i}", True) for i in range(6)],
            **{f"L{i}": [GuangYaFile(str(i), f"E{i}.mkv", False, 100, "e", f"L{i}")] for i in range(6)}}
        if stage == "deferred-cleanup":
            # 先建立真实文件/索引，正式 run 在整轮安全门之后执行延后删除。
            strm.sync_strm("source", "http://play.invalid", str(tmp_path), client=cast(GuangYaClient, _TreeClient(tree)), clean_empty_dirs=False)
            tree = {"source": []}
        cloud = _TreeClient(tree)
        scheduler = STRMScheduler()
        for name, value in [("validate_config", ""), ("_source_dirs", [source]),
                            ("_video_exts", {"mkv"}), ("_metadata_exts", set())]:
            stack.enter_context(patch.object(scheduler, name, return_value=value))
        for name in ("_notify_success", "_notify_details"):
            stack.enter_context(patch.object(scheduler, name))
        values = {"STRM_ROOT": str(tmp_path), "GY_STRM_BASE_URL": "http://play.invalid"}
        stack.enter_context(patch("app.modules.scheduler.get", side_effect=lambda k, d="": values.get(k, d)))
        stack.enter_context(patch("app.modules.scheduler.get_int", return_value=0))
        stack.enter_context(patch("app.modules.scheduler.configured_strm_source_plans", return_value=([source], "")))
        stack.enter_context(patch("app.modules.scheduler.sync_strm", side_effect=lambda **kw: strm.sync_strm(client=cast(GuangYaClient, cloud), **kw)))
        stack.enter_context(patch("app.modules.scheduler.sync_strm_incremental", side_effect=lambda **kw: strm.sync_strm_incremental(client=cast(GuangYaClient, cloud), **kw)))
        stack.enter_context(patch.object(strm, "_MAX_TRACKED_CHANGED_PATHS", 1))
        stack.enter_context(patch.object(strm, "_MAX_TRACKED_OVERFLOW_DIRS", 1))
        handoff = stack.enter_context(patch("app.modules.media_refresh_coordinator.enqueue_media_refresh_paths", return_value={"Jellyfin": "queued"}))
        execute = scheduler._execute_locked
        def execute_with_policy(trigger, run_id):
            scheduler._run_options.update(emby_refresh_override=allow_emby)
            return execute(trigger, run_id)
        stack.enter_context(patch.object(scheduler, "_execute_locked", side_effect=execute_with_policy))
        original_enqueue = strm_repo.enqueue_strm_refresh_paths
        attempts = []
        def flaky(paths, **kw):
            attempts.append(list(paths))
            if len(attempts) == 1:
                raise OSError("one-shot outbox write failure")
            return original_enqueue(paths, **kw)
        stack.enter_context(patch.object(strm_repo, "enqueue_strm_refresh_paths", side_effect=flaky))
        options: dict[str, Any] = {"sync_mode": "fast" if stage == "fast" else "full"}
        if stage == "fast":
            options["organize_changes"] = [{"source_id": "source", "kind": "video", "action": "upsert",
                "file_id": str(i), "name": f"E{i}.mkv", "rel_dir": f"L{i}", "parent_id": f"L{i}"} for i in range(6)]
        result = scheduler.run_blocking("manual", **options)
        assert result["stopped"], result
        handoff.assert_not_called()
        expected_count = 6 if stage == "deferred-cleanup" else 3
        entries = strm_repo.list_strm_refresh_entries()
        assert len(entries) == expected_count
        assert all(e["allow_emby"] is allow_emby for e in entries)
        assert set(attempts[0]) <= {e["path"] for e in entries}
        assert result["stats"]["generated"] == (0 if stage == "deferred-cleanup" else 3)
        assert len(attempts) >= 2  # 第一次失败的批次在收尾/后续持久批次中重试
