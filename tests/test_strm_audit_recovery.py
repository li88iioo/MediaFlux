"""B 工作片：真实失败台账 + 临时文件，禁止连接真实 provider。"""
from pathlib import Path
from unittest.mock import patch
import threading

import pytest

from app import database as db
from app.clients.guangya import GuangYaFile
from app.modules import strm
from tests.support import isolated_test_database
from tests.test_strm_hardening import _TreeClient


def seed(root, count=3):
    files = [GuangYaFile(f"v{i}", f"Movie{i}.mkv", False, 100, f"e{i}", "s") for i in range(count)]
    for file in files:
        db.record_strm_failure(source_id="s", source_name="S", file_id=file.file_id,
            parent_id="s", filename=file.name,
            rel_dir="", target_rel_path=f"{file.name}.strm", action="generate", error="disk failed")
    runtime = {"sources": strm.plan_strm_sources([{"id": "s", "name": "S"}]),
               "strm_root": str(root), "base_url": "http://play.invalid"}
    return _TreeClient({"s": files}), runtime


def retry(mode, client, runtime, **kwargs):
    if mode == "all":
        return strm.retry_all_strm_failures("s", "generate", "test", client=client, runtime_config=runtime, **kwargs)
    return strm.retry_strm_failures([int(row["id"]) for row in db.list_strm_failures(status="open")],
                                   "test", client=client, runtime_config=runtime, **kwargs)


@pytest.mark.parametrize("mode", ["selected", "all"])
@pytest.mark.parametrize("stop_at", ["after_scan", "after_first_install"])
def test_retry_stop_never_installs_remaining_located_files(tmp_path, mode, stop_at):
    with isolated_test_database(), patch("socket.socket.connect", side_effect=AssertionError("network forbidden")):
        client, runtime = seed(tmp_path)
        stop = threading.Event()
        install = strm._install_video_candidate

        def on_progress(stage, completed, total, detail):
            if stop_at == "after_scan" and completed == 0:
                stop.set()

        def install_once(*args, **kwargs):
            result = install(*args, **kwargs)
            if stop_at == "after_first_install":
                stop.set()
            return result

        with patch.object(strm, "_install_video_candidate", side_effect=install_once):
            result = retry(mode, client, runtime, should_stop=stop.is_set, on_progress=on_progress)
        expected = int(stop_at == "after_first_install")
        assert len(list(tmp_path.rglob("*.strm"))) == expected, result
        assert result["stopped"] and result["stop_stage"] == "retry"
        assert result["deferred"] == 3 - expected
        assert not db.list_strm_failures(status="retrying")
        stop.clear()
        again = retry(mode, client, runtime, should_stop=stop.is_set)
        assert again["resolved"] == 3 - expected
        assert len(list(tmp_path.rglob("*.strm"))) == 3


def test_retry_committed_video_persists_refresh_before_ack_and_replays(tmp_path):
    from app.modules.strm_metadata_worker import STRMMetadataWorker
    with isolated_test_database(), patch("socket.socket.connect", side_effect=AssertionError("network forbidden")):
        client, runtime = seed(tmp_path, 1)
        runtime.update(media_server_refresh=True, allow_emby=False)
        result = retry("selected", client, runtime)
        assert result["resolved"] == 1
        entries = db.list_strm_refresh_entries()
        assert len(entries) == 1, "失败重试已写盘/确认成功，但丢失媒体库刷新意图"
        assert entries[0]["allow_emby"] is False
        assert Path(entries[0]["path"]).exists()
        with patch("app.modules.media_refresh_coordinator.enqueue_media_refresh_paths", return_value={"Jellyfin": "queued"}) as handoff:
            STRMMetadataWorker()._flush_media_refresh(force=True)
            assert handoff.call_count == 1
        assert db.count_strm_refresh_paths() == 0


@pytest.mark.parametrize("count", [40, 80, 1001])
def test_metadata_retry_only_visits_related_index_rows(tmp_path, count):
    """计数安装器实际收到的索引行，排除墙钟与网络波动。"""
    with isolated_test_database(), patch("socket.socket.connect", side_effect=AssertionError("network forbidden")):
        files = [GuangYaFile(f"m{i}", f"Movie{i}.nfo", False, 8, f"e{i}", "s") for i in range(count)]
        for file in files:
            db.record_strm_failure(source_id="s", source_name="S", file_id=file.file_id,
                parent_id="s", filename=file.name, rel_dir="", target_rel_path=file.name,
                action="metadata", error="download failed")
            # 历史索引保留，但文件缺失，重试必须补写。
            target = strm._metadata_target(file, "", str(tmp_path))
            db.upsert_strm_index("guangya-meta:s", file.file_id, file.etag, file.size, file.name, str(target))
        client = _TreeClient({"s": files})
        runtime = {"sources": strm.plan_strm_sources([{"id": "s", "name": "S"}]),
                   "strm_root": str(tmp_path), "base_url": "http://play.invalid"}
        visits = []
        original = strm._install_metadata_candidate

        def download(file, rel_dir, root, client=None, **kwargs):
            target = strm._metadata_target(file, rel_dir, root)
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = strm._temporary_path(target)
            temp.write_bytes(b"<movie/>")
            return strm.PreparedMetadataDownload(target, temp, strm._content_fingerprint(temp))

        def install(*args, **kwargs):
            visits.append(len(args[5]))
            return original(*args, **kwargs)

        with patch.object(strm, "prepare_metadata_download", side_effect=download), patch.object(strm, "_install_metadata_candidate", side_effect=install), patch.object(db, "list_strm_index", wraps=db.list_strm_index) as reads:
            result = strm.retry_all_strm_failures("s", "metadata", "test", client=client, runtime_config=runtime)
        assert result["resolved"] == count, result
        assert reads.call_count == 1
        assert sum(visits) <= count * 2, {"n": count, "visited": sum(visits)}


def test_retry_refresh_ack_is_atomic_and_failure_remains_replayable(tmp_path):
    import sqlite3
    from app.repositories import strm as repository
    with isolated_test_database(), patch("socket.socket.connect", side_effect=AssertionError("network forbidden")):
        client, runtime = seed(tmp_path, 1)
        with patch.object(repository, "_enqueue_strm_refresh_paths", side_effect=sqlite3.OperationalError("outbox unavailable")):
            first = retry("selected", client, runtime)
        assert first["resolved"] == 0 and first["failed"] == 1
        assert len(db.list_strm_failures(status="open")) == 1
        assert not db.list_strm_failures(status="resolved")
        assert len(list(tmp_path.rglob("*.strm"))) == 1
        again = retry("selected", client, runtime)
        assert again["resolved"] == 1
        assert db.count_strm_refresh_paths() == 1
        assert len(list(tmp_path.rglob("*.strm"))) == 1


def test_retry_refresh_disabled_does_not_publish_work(tmp_path):
    with isolated_test_database(), patch("socket.socket.connect", side_effect=AssertionError("network forbidden")):
        client, runtime = seed(tmp_path, 1)
        runtime["media_server_refresh"] = False
        assert retry("selected", client, runtime)["resolved"] == 1
        assert db.count_strm_refresh_paths() == 0


def test_retry_batches_flow_through_durable_refresh_coordinator(tmp_path):
    """两个来源同名文件 → 失败恢复 → outbox → 合并刷新，第二次消费无重复。"""
    from unittest.mock import Mock
    from app.modules import media_refresh_coordinator as refresh
    from app.modules.strm_metadata_worker import STRMMetadataWorker
    from app.repositories import media_refresh_queue as queue
    with isolated_test_database(), patch("socket.socket.connect", side_effect=AssertionError("network forbidden")):
        plans = strm.plan_strm_sources([{"id": "s1", "name": "Films"}, {"id": "s2", "name": "Films"}])
        files = [GuangYaFile(f"v{i}", "Same.mkv", False, 100, f"e{i}", f"s{i}") for i in (1, 2)]
        client = _TreeClient({file.parent_id: [file] for file in files})
        runtime = {"sources": plans, "strm_root": str(tmp_path), "base_url": "http://play.invalid"}
        for file in files:
            db.record_strm_failure(source_id=file.parent_id, source_name="Films", file_id=file.file_id,
                parent_id=file.parent_id, filename=file.name, rel_dir="", target_rel_path=file.name,
                action="generate", error="previous failure")
        for file in files:
            result = strm.retry_all_strm_failures(file.parent_id, "generate", "test", client=client, runtime_config=runtime)
            assert result["resolved"] == 1
        paths = sorted(tmp_path.rglob("*.strm"))
        assert len(paths) == 2 and paths[0].read_bytes() != paths[1].read_bytes()
        assert db.count_strm_refresh_paths() == 2
        coordinator = refresh.MediaRefreshCoordinator()
        provider = Mock(display_name="Jellyfin")
        provider.refresh_for_paths.return_value = {"ok": True, "succeeded_target_ids": ["library"], "deduplicated": 0}
        with patch.object(refresh, "_configured_provider_names", return_value=("jellyfin",)), patch.object(refresh, "get_media_refresh_coordinator", return_value=coordinator), patch.object(coordinator, "wake"):
            STRMMetadataWorker()._flush_media_refresh(force=True)
        groups = queue.claim_due_media_refreshes(owner=coordinator._owner, limit=8)
        assert len(groups) == 1
        assert set(groups[0]["paths"]) == {str(path) for path in paths}
        with patch.object(coordinator, "_client_for", return_value=provider), patch("app.services.clear_dashboard_cache"):
            coordinator._process_group(groups[0])
        provider.refresh_for_paths.assert_called_once()
        assert provider.refresh_for_paths.call_args.kwargs["allow_global_fallback"] is False
        assert not queue.claim_due_media_refreshes(owner=coordinator._owner, limit=8)
        assert db.count_strm_refresh_paths() == 0


def test_metadata_retry_download_stop_releases_instead_of_failing(tmp_path):
    with isolated_test_database(), patch("socket.socket.connect", side_effect=AssertionError("network forbidden")):
        file = GuangYaFile("m", "Movie.nfo", False, 8, "e", "s")
        db.record_strm_failure(source_id="s", source_name="S", file_id="m", parent_id="s", filename=file.name,
            action="metadata", rel_dir="", target_rel_path=file.name, error="old failure")
        runtime = {"sources": strm.plan_strm_sources([{"id": "s", "name": "S"}]),
                   "strm_root": str(tmp_path), "base_url": "http://play.invalid"}
        stop = threading.Event()

        def download(*args, **kwargs):
            assert kwargs["should_stop"] is not None
            stop.set()
            raise strm._STRMStopped("cancelled during download")

        with patch.object(strm, "prepare_metadata_download", side_effect=download):
            result = strm.retry_all_strm_failures("s", "metadata", "test", client=_TreeClient({"s": [file]}), runtime_config=runtime, should_stop=stop.is_set)
        assert result["stopped"] and result["deferred"] == 1
        assert result["failed"] == 0
        row = db.list_strm_failures(status="open")[0]
        assert row["failure_count"] == 1
        assert not list(tmp_path.rglob("*.nfo"))
