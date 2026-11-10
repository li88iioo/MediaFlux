"""STRM 落盘后的正常停止必须保留刷新意图，不在关停期间联网。"""

from typing import cast
from app.clients.guangya import GuangYaClient
from contextlib import ExitStack
from unittest.mock import patch

import pytest

from app.repositories import strm as strm_repo
from app.clients.guangya import GuangYaFile
from app.modules import strm
from app.modules.scheduler import STRMScheduler
from app.modules.strm_metadata_worker import STRMMetadataWorker
from tests.support import isolated_test_database
from tests.test_strm_hardening import _TreeClient


@pytest.mark.parametrize("mode", ["full", "fast"])
@pytest.mark.parametrize("refresh_enabled,allow_emby", [(True, True), (True, False), (False, False)])
def test_committed_strm_stop_retains_refresh_for_skip_only_replay(tmp_path, mode, refresh_enabled, allow_emby):
    with isolated_test_database(), ExitStack() as stack:
        stack.enter_context(patch("socket.socket.connect", side_effect=AssertionError("network forbidden")))
        scheduler = STRMScheduler()
        source = {"id": "source", "name": "source", "rel_prefix": "Films"}
        media = GuangYaFile(file_id="v1", name="Movie.mkv", is_dir=False,
                            etag="e1", size=128, parent_id="source")
        client = _TreeClient({"source": [media]})
        values = {"STRM_ROOT": str(tmp_path), "GY_STRM_BASE_URL": "http://media.invalid"}
        for name, value in [("validate_config", ""), ("_source_dirs", [source]),
                            ("_video_exts", {"mkv"}), ("_metadata_exts", set())]:
            stack.enter_context(patch.object(scheduler, name, return_value=value))
        for name in ("_notify_success", "_notify_details"):
            stack.enter_context(patch.object(scheduler, name))
        stack.enter_context(patch("app.modules.scheduler.get", side_effect=lambda k, d="": values.get(k, d)))
        stack.enter_context(patch("app.modules.scheduler.get_int", return_value=0))
        stack.enter_context(patch("app.modules.scheduler.configured_strm_source_plans", return_value=([source], "")))
        stack.enter_context(patch("app.modules.scheduler.sync_strm_incremental", side_effect=lambda **kw: strm.sync_strm_incremental(client=cast(GuangYaClient, client), **kw)))
        stack.enter_context(patch("app.modules.scheduler.sync_strm", side_effect=lambda **kw: strm.sync_strm(client=cast(GuangYaClient, client), **kw)))
        refresh = stack.enter_context(patch("app.modules.media_refresh_coordinator.enqueue_media_refresh_paths", return_value={"Jellyfin": "queued"}))
        original = strm._track_change

        def stop_after_commit(*args, **kwargs):
            original(*args, **kwargs)
            if args[1] == "generated":
                scheduler._stop_event.set()

        change = {"source_id": "source", "kind": "video", "action": "upsert", "file_id": "v1",
                  "parent_id": "source", "name": "Movie.mkv", "rel_dir": ""}
        execute = scheduler._execute_locked

        def execute_with_policy(trigger_type, run_id):
            scheduler._run_options.update(
                media_server_refresh_override=refresh_enabled,
                emby_refresh_override=allow_emby,
            )
            return execute(trigger_type, run_id)

        stack.enter_context(patch.object(scheduler, "_execute_locked", side_effect=execute_with_policy))
        options = {"sync_mode": mode}
        if mode == "fast":
            options["organize_changes"] = [change]
        with patch.object(strm, "_track_change", side_effect=stop_after_commit):
            first = scheduler.run_blocking("manual", **options)
        assert first["stopped"] and first["stats"]["generated"] == 1
        refresh.assert_not_called()
        entries = strm_repo.list_strm_refresh_entries()
        assert len(entries) == int(refresh_enabled)
        if entries:
            assert entries[0]["allow_emby"] is allow_emby
        if mode == "fast":
            assert strm_repo.count_pending_strm_change_targets() == 1
        scheduler._stop_event.clear()
        second = scheduler.run_blocking("manual", sync_mode=mode)
        assert second["stats"]["generated"] == 0 and second["stats"]["skipped"] == 1
        assert strm_repo.count_pending_strm_change_targets() == 0
        # 全跳过不会新建刷新事件；由独立 worker 接管上次已提交的文件变化。
        refresh.assert_not_called()
        STRMMetadataWorker()._flush_media_refresh(force=True)
        assert refresh.call_count == int(refresh_enabled)
        assert strm_repo.count_strm_refresh_paths() == 0
