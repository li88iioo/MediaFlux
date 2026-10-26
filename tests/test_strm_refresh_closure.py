"""STRM 提交、恢复与刷新交接反例；临时 SQLite / 假云盘，禁止外网。"""
from __future__ import annotations

from typing import cast
from app.clients.guangya import GuangYaClient

import json
import sqlite3
import subprocess
import sys
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from app import config
from app import database as db
from app.clients.guangya import GuangYaFile
from app.modules import media_refresh_coordinator as refresh
from app.modules import scheduler as scheduler_module
from app.modules import strm
from app.modules.media_refresh import plan_refresh_targets
from app.modules.scheduler import STRMScheduler
from app.modules.strm_metadata_worker import STRMMetadataWorker
from app.repositories import strm as repository
from app.repositories.media_refresh_queue import (
    claim_due_media_refreshes,
    media_refresh_queue_status,
)
from tests.support import isolated_test_database
from tests.test_strm_hardening import _TreeClient


def _tree(*, flat=False, etag="e"):
    media = GuangYaFile("v", "Movie.mkv", False, 100, etag, "s" if flat else "d")
    return {"s": [media]} if flat else {
        "s": [GuangYaFile("d", "Film", True, parent_id="s")], "d": [media],
    }


def _run(root, *, mode="full", enabled=True, allow_emby=True, tree=None,
         flat=False, retire=False, configured_text=None, queue_only=False):
    """经真实 scheduler 注入策略；不直接为安装器伪造刷新行为。"""
    scheduler = STRMScheduler()
    sources = [] if retire else strm.plan_strm_sources([{"id": "s", "name": "S"}])
    values = {
        "STRM_ROOT": str(root), "GY_STRM_BASE_URL": "http://play.invalid",
        "GY_STRM_SOURCE_DIRS": configured_text if configured_text is not None else json.dumps(
            [{"id": item["id"], "name": item["name"]} for item in sources]
        ),
    }
    client = _TreeClient(_tree(flat=flat) if tree is None else tree)
    with ExitStack() as stack:
        stack.enter_context(patch.object(scheduler_module, "get", side_effect=lambda k, d="": values.get(k, d)))
        stack.enter_context(patch.object(scheduler_module, "get_int", return_value=0))
        stack.enter_context(patch.object(scheduler_module, "configured_strm_source_plans", return_value=(sources, "" if sources else "来源为空")))
        for name, value in (("validate_config", ""), ("_source_dirs", sources),
                            ("_video_exts", {"mkv"}), ("_metadata_exts", set())):
            stack.enter_context(patch.object(scheduler, name, return_value=value))
        for name in ("_notify_success", "_notify_details", "_notify_failure", "_schedule_persisted_change_queue"):
            stack.enter_context(patch.object(scheduler, name))
        stack.enter_context(patch.object(scheduler_module, "sync_strm", side_effect=lambda **kw: strm.sync_strm(client=cast(GuangYaClient, client), **kw)))
        stack.enter_context(patch.object(scheduler_module, "sync_strm_incremental", side_effect=lambda **kw: strm.sync_strm_incremental(client=cast(GuangYaClient, client), **kw)))
        execute = scheduler._execute_locked

        def execute_with_policy(trigger, run_id):
            scheduler._run_options.update(media_server_refresh_override=enabled, emby_refresh_override=allow_emby)
            return execute(trigger, run_id)

        stack.enter_context(patch.object(scheduler, "_execute_locked", side_effect=execute_with_policy))
        changes = [{"source_id": "s", "kind": "video", "action": "upsert", "file_id": "v",
                    "parent_id": "s" if flat else "d", "name": "Movie.mkv", "rel_dir": "" if flat else "Film"}]
        return scheduler.run_blocking("manual", sync_mode=mode, organize_changes=changes if mode == "fast" and not queue_only else None)


@contextmanager
def _providers(*names):
    values = {"JELLYFIN_URL": "http://jellyfin.invalid", "JELLYFIN_API_KEY": "synthetic",
              "EMBY_URL": "http://emby.invalid", "EMBY_TOKEN": "synthetic"}
    get, get_bool = refresh.config.get, refresh.config.get_bool
    with patch.object(refresh.config, "get_bool", side_effect=lambda k, d=False: (
             k.removesuffix("_ENABLED").lower() in names
             if k in {"JELLYFIN_ENABLED", "EMBY_ENABLED"} else get_bool(k, d)
         )), patch.object(refresh.config, "get", side_effect=lambda k, d="": values[k] if k in values else get(k, d)), \
         patch.object(refresh, "get_media_refresh_coordinator", return_value=Mock()):
        yield


def _consume(*, providers, expected_paths):
    with _providers(*providers):
        STRMMetadataWorker()._flush_media_refresh(force=True)
    groups = claim_due_media_refreshes(owner="closure-test", limit=8)
    assert {group["provider"] for group in groups} == set(providers)
    coordinator = refresh.MediaRefreshCoordinator()
    coordinator._owner = "closure-test"
    for group in groups:
        assert set(group["paths"]) == set(expected_paths)
        client = Mock(display_name=group["provider"])
        client.refresh_for_paths.return_value = {"ok": True, "deduplicated": 0, "succeeded_target_ids": ["item"]}
        with patch.object(coordinator, "_client_for", return_value=client), patch("app.services.clear_dashboard_cache"):
            coordinator._process_group(group)
        assert client.refresh_for_paths.call_args.kwargs["allow_global_fallback"] is False
    assert db.count_strm_refresh_paths() == 0
    assert media_refresh_queue_status()["paths"] == 0


_CHILD = r'''
import os, socket, sys
from pathlib import Path
from unittest.mock import patch
# 在应用导入前封闭子进程网络；不依赖本机未入库的执行脚本。
def deny_network(event, args):
    if event in ('socket.getaddrinfo', 'socket.gethostbyname'):
        raise AssertionError('network disabled in crash-recovery subprocess')
    if event in ('socket.connect', 'socket.bind', 'socket.sendto') and args[0].family != socket.AF_UNIX:
        raise AssertionError('network disabled in crash-recovery subprocess')

sys.addaudithook(deny_network)
import tests
from app import database as db
from tests.test_strm_refresh_closure import _run, _providers

db.configure_database(Path(sys.argv[1]), test_mode=True)
original = db.upsert_strm_index

def crash_after_commit(*args, **kwargs):
    original(*args, **kwargs)
    os._exit(73)

with _providers(), patch.object(db, 'upsert_strm_index', side_effect=crash_after_commit):
    _run(Path(sys.argv[2]), mode=sys.argv[3], enabled=sys.argv[4]=='1', allow_emby=sys.argv[5]=='1')
os._exit(74)
'''


@pytest.mark.parametrize("mode", ["full", "fast"])
@pytest.mark.parametrize("enabled,allow_emby", [(True, True), (True, False), (False, False)])
def test_commit_crash_replays_refresh_without_rewriting_current_strm(tmp_path, mode, enabled, allow_emby):
    with isolated_test_database() as database_path, _providers():
        child = subprocess.run(
            [sys.executable, "-c", _CHILD, str(database_path), str(tmp_path), mode, str(int(enabled)), str(int(allow_emby))],
            capture_output=True, text=True, timeout=30, check=False,
        )
        assert child.returncode == 73, (child.returncode, child.stdout, child.stderr)
        paths = list(tmp_path.rglob("*.strm"))
        assert len(paths) == len(db.list_strm_index("guangya:s")) == 1
        entries = db.list_strm_refresh_entries()
        assert len(entries) == int(enabled)
        if entries:
            assert entries[0]["allow_emby"] is allow_emby
        before = paths[0].read_bytes()
        replay = _run(tmp_path, enabled=enabled, allow_emby=allow_emby)
        assert replay["stats"]["generated"] == 0 and replay["stats"]["skipped"] == 1
        assert paths[0].read_bytes() == before
        assert db.list_strm_refresh_entries() == entries
        if enabled:
            _consume(providers=("jellyfin", "emby") if allow_emby else ("jellyfin",), expected_paths=[str(paths[0].parent)])
        else:
            with _providers("jellyfin", "emby"):
                STRMMetadataWorker()._flush_media_refresh(force=True)
            assert media_refresh_queue_status()["paths"] == 0


@pytest.mark.parametrize("origin", ["outbox", "foreground"])
def test_empty_provider_keeps_intent_until_configuration_recovers(tmp_path, origin):
    target = str(tmp_path / strm.STRM_SUBDIR / "Film")
    with isolated_test_database(), _providers():
        if origin == "outbox":
            db.enqueue_strm_refresh_paths([target], allow_emby=False)
            worker = STRMMetadataWorker()
            worker._flush_media_refresh(force=True)
            assert worker._refresh_retry_pending
        else:
            with patch.object(scheduler_module, "get", side_effect=lambda k, d="": str(tmp_path) if k == "STRM_ROOT" else d):
                result = STRMScheduler._refresh_media_servers(changed_dirs=[target], emby_enabled=False)
            assert result == {"媒体库": "pending"}
        assert db.count_strm_refresh_paths() == 1
        assert media_refresh_queue_status()["paths"] == 0
        _consume(providers=("jellyfin",), expected_paths=[target])


@pytest.mark.parametrize("removal", ["cloud-delete", "retirement"])
def test_single_source_root_file_create_delete_and_refresh(tmp_path, removal):
    with isolated_test_database(), _providers():
        created = _run(tmp_path, flat=True, allow_emby=False)
        path = tmp_path / strm.STRM_SUBDIR / "Movie.strm"
        assert created["stats"]["generated"] == 1 and path.is_file()
        _consume(providers=("jellyfin",), expected_paths=[str(path)])
        if removal == "retirement":
            db.enqueue_strm_retired_source("s", "S", str(tmp_path))
        deleted = _run(tmp_path, flat=True, allow_emby=False, tree={"s": []}, retire=removal == "retirement")
        assert deleted["stats"]["cleaned"] == 1
        assert not path.exists() and not db.list_strm_index("guangya:s")
        assert not db.list_strm_retired_sources()
        _consume(providers=("jellyfin",), expected_paths=[str(path)])


@pytest.mark.parametrize("existing", [False, True])
def test_failed_file_index_outbox_commit_retries_instead_of_becoming_current(tmp_path, existing):
    with isolated_test_database(), _providers():
        path = tmp_path / strm.STRM_SUBDIR / "Film" / "Movie.strm"
        if existing:
            _run(tmp_path, enabled=False, tree=_tree(etag="old"))
        before = path.read_bytes() if existing else None
        original = repository._enqueue_strm_refresh_paths

        def fail_after_outbox_write(*args, **kwargs):
            # 文件已写、索引与 outbox SQL 都已执行，但事务尚未提交。
            assert path.exists()
            original(*args, **kwargs)
            raise sqlite3.OperationalError("commit unavailable")

        with patch.object(repository, "_enqueue_strm_refresh_paths", side_effect=fail_after_outbox_write):
            result = _run(tmp_path)
        assert result["stats"]["failed"] == 1
        assert len(db.list_strm_index("guangya:s")) == int(existing)
        assert (path.read_bytes() if path.exists() else None) == before
        assert db.count_strm_refresh_paths() == 0
        replay = _run(tmp_path, allow_emby=False)
        assert replay["stats"]["generated"] == 1 and replay["stats"]["skipped"] == 0
        assert db.list_strm_index("guangya:s")[0]["etag"] == "e"
        _consume(providers=("jellyfin",), expected_paths=[str(path.parent)])


def test_deleted_file_keeps_index_until_refresh_is_durable(tmp_path):
    with isolated_test_database(), _providers():
        _run(tmp_path, enabled=False)
        with patch.object(repository, "_enqueue_strm_refresh_paths", side_effect=sqlite3.OperationalError("outbox unavailable")):
            _run(tmp_path, tree={"s": []})
        assert not list(tmp_path.rglob("*.strm"))
        assert len(db.list_strm_index("guangya:s")) == 1
        assert db.count_strm_refresh_paths() == 0
        replay = _run(tmp_path, tree={"s": []}, allow_emby=False)
        assert replay["stats"]["cleaned"] == 0
        assert not db.list_strm_index("guangya:s")
        _consume(providers=("jellyfin",), expected_paths=[str(tmp_path / strm.STRM_SUBDIR / "Film")])


def test_root_file_fallback_does_not_allow_root_or_outside_directory_refresh():
    root = "/strm/光鸭云盘"
    plan = plan_refresh_targets([root + "/Movie.strm", "/outside/Movie.strm"], [root, "/outside"], media_roots=[root])
    assert plan.targets == (root + "/Movie.strm",)
    assert not plan_refresh_targets(changed_dirs=[root], media_roots=[root]).has_targets


def test_invalid_empty_source_config_does_not_unlock_retirement(tmp_path):
    with isolated_test_database(), _providers():
        _run(tmp_path, enabled=False, flat=True)
        db.enqueue_strm_retired_source("s", "S", str(tmp_path))
        _run(tmp_path, retire=True, configured_text="[broken")
        assert len(list(tmp_path.rglob("*.strm"))) == 1
        assert len(db.list_strm_index("guangya:s")) == 1
        assert len(db.list_strm_retired_sources()) == 1


@pytest.mark.parametrize("retire", [False, True])
def test_delete_commit_crash_retains_refresh_after_index_disappears(tmp_path, retire):
    child_code = _CHILD.replace(
        "original = db.upsert_strm_index", "original = db.delete_strm_index_ids"
    ).replace(
        "patch.object(db, 'upsert_strm_index'", "patch.object(db, 'delete_strm_index_ids'"
    ).replace(
        "_run(Path(sys.argv[2]), mode=sys.argv[3], enabled=sys.argv[4]=='1', allow_emby=sys.argv[5]=='1')",
        "_run(Path(sys.argv[2]), tree={'s': []}, retire=sys.argv[3]=='retire', allow_emby=False)",
    )
    with isolated_test_database() as database_path, _providers():
        _run(tmp_path, enabled=False)
        if retire:
            db.enqueue_strm_retired_source("s", "S", str(tmp_path))
        child = subprocess.run(
            [sys.executable, "-c", child_code, str(database_path), str(tmp_path), "retire" if retire else "delete"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        assert child.returncode == 73, (child.returncode, child.stdout, child.stderr)
        assert not list(tmp_path.rglob("*.strm"))
        assert not db.list_strm_index("guangya:s")
        assert db.count_strm_refresh_paths() == 1
        replay = _run(tmp_path, tree={"s": []}, retire=retire, allow_emby=False)
        assert replay["stats"]["generated"] == replay["stats"]["cleaned"] == 0
        assert not db.list_strm_retired_sources()
        _consume(providers=("jellyfin",), expected_paths=[str(tmp_path / strm.STRM_SUBDIR / "Film")])


def test_move_from_root_keeps_old_file_refresh_until_recovery_handoff(tmp_path):
    with isolated_test_database(), _providers():
        _run(tmp_path, flat=True, enabled=False)
        previous = tmp_path / strm.STRM_SUBDIR / "Movie.strm"
        result = _run(tmp_path, allow_emby=False)
        assert result["stats"]["generated"] == result["stats"]["cleaned"] == 1
        assert not previous.exists()
        assert not db.list_strm_path_cleanup("guangya:s")
        _consume(providers=("jellyfin",), expected_paths=[str(previous), str(previous.parent / "Film")])


def test_skipped_provider_is_not_a_durable_handoff(tmp_path):
    with isolated_test_database():
        db.enqueue_strm_refresh_paths([str(tmp_path / "Film")], allow_emby=False)
        with patch.object(refresh, "enqueue_media_refresh_paths", return_value={"Jellyfin": "skipped"}):
            STRMMetadataWorker()._flush_media_refresh(force=True)
        assert db.count_strm_refresh_paths() == 1
        assert media_refresh_queue_status()["paths"] == 0


@pytest.mark.parametrize("entry", ["config-trigger", "start-cron"])
def test_real_scheduler_entry_retires_last_source_without_config_short_circuit(tmp_path, entry):
    with isolated_test_database(), ExitStack() as stack:
        # 真正的索引/文件事实；随后仅配置变为空，没有伪造来源解析或校验。
        strm.sync_strm("s", "http://play.invalid", str(tmp_path), client=cast(GuangYaClient, _TreeClient(_tree(flat=True))))
        db.enqueue_strm_retired_source("s", "S", str(tmp_path))
        stack.enter_context(patch.object(config, "_cache", {
            "GY_STRM_SOURCE_DIRS": "[]", "STRM_ROOT": str(tmp_path),
            "GY_STRM_BASE_URL": "", "STRM_SCHEDULE_ENABLED": "1" if entry == "start-cron" else "0",
            "STRM_SCHEDULE_CRON": "* * * * *", "JELLYFIN_ENABLED": "0", "EMBY_ENABLED": "0",
        }))
        scheduler = STRMScheduler()
        for name in ("_notify_success", "_notify_details", "_notify_failure",
                     "_recover_notification_outbox", "_drain_notification_outbox"):
            stack.enter_context(patch.object(scheduler, name))
        # 外围消费者不启动；scheduler 自己的 loop/worker/校验均真实运行。
        for module, getter in (("strm_metadata_worker", "get_strm_metadata_worker"),
                               ("organize_probe_worker", "get_organize_probe_worker")):
            stack.enter_context(patch(f"app.modules.{module}.{getter}", return_value=Mock()))
        assert scheduler._source_dirs() == []
        assert scheduler.validate_config(auto_only=entry == "start-cron") == ""
        try:
            if entry == "start-cron":
                # scheduler 使用本机无时区时间；显式转换以保持同一时间契约。
                local_now = datetime.now(tz=timezone.utc).astimezone().replace(tzinfo=None)
                scheduler._next_run = local_now - timedelta(seconds=1)
                scheduler._loaded_cron = "* * * * *"
                scheduler.start()
            else:
                assert scheduler.trigger("config-retirement")["ok"]
            assert scheduler._run_released_event.wait(5), "真实 scheduler 入口未完成退役"
            run = db.get_last_task_run("strm_sync")
            assert run is not None
            assert run["trigger_type"] == ("cron" if entry == "start-cron" else "config-retirement")
            result = json.loads(run["result"])
            assert result["stats"]["cleaned"] == result["stats"]["retired_sources"] == 1
            assert not db.list_strm_index("guangya:s") and not db.list_strm_retired_sources()
            assert not list(tmp_path.rglob("*.strm"))
            assert db.count_strm_refresh_paths() == 1
        finally:
            assert scheduler.stop(timeout=5)
        _consume(providers=("jellyfin",), expected_paths=[str(tmp_path / strm.STRM_SUBDIR / "Movie.strm")])


def test_commit_and_overflow_share_frozen_policy_and_do_not_duplicate_paths(tmp_path):
    with isolated_test_database():
        scheduler = STRMScheduler()
        scheduler._run_options = {"media_server_refresh_override": True, "emby_refresh_override": False}
        first = scheduler._refresh_overflow_sink(str(tmp_path / "first"))
        path = Path(first.media_root) / "Film" / "Movie.strm"
        db.upsert_strm_index("guangya:s", "v", "e", 1, "Movie.mkv", str(path), refresh_policy=first)
        first([str(path.parent)])
        assert db.count_strm_refresh_paths() == 1
        scheduler._run_options = {"media_server_refresh_override": False, "emby_refresh_override": True}
        disabled = scheduler._refresh_overflow_sink(str(tmp_path / "second"))
        disabled([str(Path(disabled.media_root) / "Film")])
        # 后一轮开关/根改变，不能污染上一轮已捕获策略。
        first([str(path.parent)])
        entries = db.list_strm_refresh_entries()
        assert len(entries) == 1 and entries[0]["allow_emby"] is False
        assert entries[0]["path"] == str(path.parent)
        scheduler._run_options = {"media_server_refresh_override": True, "emby_refresh_override": True}
        second = scheduler._refresh_overflow_sink(str(tmp_path / "second"))
        second([str(Path(second.media_root) / "Film")])
        assert {(entry["path"], entry["allow_emby"]) for entry in db.list_strm_refresh_entries()} == {
            (str(path.parent), False), (str(Path(second.media_root) / "Film"), True),
        }


def test_notification_pending_refresh_label_is_localized_and_not_failed():
    label = scheduler_module._refresh_notification_label({"媒体库": "pending", "Emby": "queued"})
    assert label == "媒体库 待重试（等待配置恢复） / Emby 已排队"


@pytest.mark.parametrize("refresh_result", [
    {"媒体库": "pending"},
    {"Jellyfin": True, "Emby": "pending"},
    {"Jellyfin": "queued", "Emby": "pending"},
])
def test_pending_refresh_event_is_partial_without_generation_failures(refresh_result):
    event = STRMScheduler._build_success_event(
        {"generated": 1, "failed": 0}, refresh_result, 1.0, "manual",
        [{"id": "s", "name": "S"}], "/synthetic/strm",
    )
    assert event.title == "⚠️ STRM 同步部分完成"
    text = dict(event.fields)["媒体库刷新"]
    assert isinstance(text, str)
    assert "待重试（等待配置恢复）" in text
    assert "pending" not in text and "❌" not in text


@pytest.mark.parametrize("restricted", [False, True])
def test_notification_pending_standalone_publish_cannot_claim_completion(tmp_path, restricted):
    with isolated_test_database(), _providers():
        result = _run(tmp_path, allow_emby=False)
        assert result["partial"] and not result["strm_partial"]
        assert result["media_refresh"] == {"媒体库": "pending"}
        recipients = ["100", "200"] if restricted else ["100"]
        with patch("app.modules.telegram_notification_center.publish_notification_event") as publish:
            STRMScheduler._notify_success(
                result["stats"], result["media_refresh"], 1.0, "manual",
                [{"id": "s", "name": "S"}], "/synthetic/private-root",
                notify_override=True, chat_ids=recipients,
                uses_default_notification_scope=False,
            )
        assert publish.call_count == len(recipients)
        for call in publish.call_args_list:
            event = call.args[1]
            assert event.title == "⚠️ STRM 同步部分完成"
            assert call.kwargs["importance"].name == "ERROR"
            if restricted:
                assert "private-root" not in str(event)
            else:
                assert dict(event.fields)["媒体库刷新"] == "媒体库 待重试（等待配置恢复）"


def test_crash_child_network_guard_runs_without_repository_or_temporary_helpers(tmp_path):
    bootstrap = _CHILD.split("\nimport tests\n", 1)[0]
    child = subprocess.run(
        [sys.executable, "-c", bootstrap + "\ntry:\n    socket.getaddrinfo('network.invalid', 443)\nexcept AssertionError as exc:\n    assert str(exc) == 'network disabled in crash-recovery subprocess'\nelse:\n    raise AssertionError('DNS was not blocked')\n"],
        cwd=tmp_path, capture_output=True, text=True, timeout=10, check=False,
    )
    assert child.returncode == 0, (child.stdout, child.stderr)
