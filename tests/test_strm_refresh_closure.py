"""STRM 提交、恢复与刷新交接反例；临时 SQLite / 假云盘，禁止外网。"""
from __future__ import annotations

from typing import cast
from app.clients.guangya import GuangYaClient

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

import pytest

from app import config
from app import database as db
from app.clients.guangya import GuangYaFile
from app.modules import media_refresh_coordinator as refresh
from app.modules import scheduler as scheduler_module
from app.modules import strm, telegram_notification_center as notification_center
from app.modules.media_refresh import plan_refresh_targets
from app.modules.scheduler import STRMScheduler
from app.modules.strm_metadata_worker import STRMMetadataWorker
from app.notifier import TelegramSendResult
from app.repositories import strm as repository
from app.repositories.media_refresh_queue import (
    claim_due_media_refreshes,
    media_refresh_queue_status,
)
from tests.support import isolated_test_database, media_refresh_providers
from tests.test_strm_hardening import _TreeClient


def _tree(*, flat=False, etag="e"):
    media = GuangYaFile("v", "Movie.mkv", False, 100, etag, "s" if flat else "d")
    return {"s": [media]} if flat else {
        "s": [GuangYaFile("d", "Film", True, parent_id="s")], "d": [media],
    }


def _run(root, *, mode="full", enabled=True, allow_emby=True, tree=None,
         flat=False, retire=False, configured_text=None, queue_only=False,
         trigger_type="manual", metadata_exts=None, run_options=None, send_summary=False,
         threshold_mb=0):
    """经真实 scheduler 注入策略；不直接为安装器伪造刷新行为。"""
    scheduler = STRMScheduler()
    sources = [] if retire else strm.plan_strm_sources([{"id": "s", "name": "S"}], prefixes={"s": ""})
    values = {
        "STRM_ROOT": str(root), "GY_STRM_BASE_URL": "http://play.invalid",
        "GY_STRM_SOURCE_DIRS": configured_text if configured_text is not None else json.dumps(
            [{"id": item["id"], "name": item["name"]} for item in sources]
        ),
    }
    if threshold_mb is not None:
        values["STRM_SKIP_THRESHOLD_MB"] = str(threshold_mb)
    client = _TreeClient(_tree(flat=flat) if tree is None else tree)
    with ExitStack() as stack:
        stack.enter_context(patch.object(scheduler_module, "get", side_effect=lambda k, d="": values.get(k, d)))
        stack.enter_context(patch.object(scheduler_module, "get_int", side_effect=lambda k, d=0: int(values.get(k, d))))
        stack.enter_context(patch.object(scheduler_module, "configured_strm_source_plans", return_value=(sources, "" if sources else "来源为空")))
        for name, value in (("validate_config", ""), ("_source_dirs", sources),
                            ("_video_exts", {"mkv"}), ("_metadata_exts", set(metadata_exts or ()))):
            stack.enter_context(patch.object(scheduler, name, return_value=value))
        for name in ("_notify_success", "_notify_details", "_notify_failure", "_schedule_persisted_change_queue"):
            if name == "_notify_success" and send_summary:
                continue
            stack.enter_context(patch.object(scheduler, name))
        stack.enter_context(patch.object(scheduler_module, "sync_strm", side_effect=lambda **kw: strm.sync_strm(client=cast(GuangYaClient, client), **kw)))
        stack.enter_context(patch.object(scheduler_module, "sync_strm_incremental", side_effect=lambda **kw: strm.sync_strm_incremental(client=cast(GuangYaClient, client), **kw)))
        execute = scheduler._execute_locked
        freeze_policy = scheduler._refresh_overflow_sink
        # 历史 provider 边界直接装入冻结策略，不恢复已下线的运行时单 provider 开关。
        stack.enter_context(patch.object(scheduler, "_refresh_overflow_sink", side_effect=lambda root=None: replace(
            freeze_policy(root), allow_emby=allow_emby,
        )))

        def execute_with_policy(trigger, run_id):
            scheduler._run_options.update(media_server_refresh_override=enabled)
            scheduler._run_options.update(run_options or {})
            return execute(trigger, run_id)

        stack.enter_context(patch.object(scheduler, "_execute_locked", side_effect=execute_with_policy))
        changes = [{"source_id": "s", "kind": "video", "action": "upsert", "file_id": "v",
                    "parent_id": "s" if flat else "d", "name": "Movie.mkv", "rel_dir": "" if flat else "Film"}]
        return scheduler.run_blocking(trigger_type, sync_mode=mode, organize_changes=changes if mode == "fast" and not queue_only else None)


@pytest.mark.parametrize("mode", ["full", "fast"])
@pytest.mark.parametrize(("threshold_mb", "size", "generated"), [
    (None, 10 * 1024**2 - 1, False),
    (None, 10 * 1024**2, True),
    (0, 10 * 1024**2 - 1, True),
])
def test_strm_scheduler_applies_default_size_filter_and_preserves_unlimited(tmp_path, mode, threshold_mb, size, generated):
    tree = {"s": [GuangYaFile("v", "Movie.mkv", False, size, "etag", "s")]}
    with isolated_test_database():
        result = _run(tmp_path, mode=mode, flat=True, tree=tree, enabled=False, threshold_mb=threshold_mb)
        assert result["ok"], result
        assert result["mode"] == mode
        assert (tmp_path / strm.STRM_SUBDIR / "Movie.strm").is_file() is generated


@contextmanager
def _providers(*names):
    with media_refresh_providers(*names), patch.object(refresh, "get_media_refresh_coordinator", return_value=Mock()):
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
    assert repository.count_strm_refresh_paths() == 0
    assert media_refresh_queue_status()["paths"] == 0


@pytest.mark.parametrize("level", ["essential", "standard"])
def test_refresh_failure_updates_original_run_and_notifies_original_chat(tmp_path, level):
    saved_chat = "123"
    with (
        isolated_test_database(), _providers("jellyfin"),
        patch.object(notification_center, "_dispatch_stop", Event()),
        patch.dict(os.environ, {
            "STRM_NOTIFY_ENABLED": "1", "TG_NOTIFICATION_ENABLED": "1", "TG_NOTIFICATION_LEVEL": level,
        }),
        patch.object(notification_center, "send_event_result", return_value=TelegramSendResult(True, message_id=73)) as send,
        patch.object(notification_center, "edit_event_result", return_value=TelegramSendResult(True, message_id=73)) as edit,
    ):
        result = _run(tmp_path, run_options={"notification_scopes": [{"chat_ids": [saved_chat]}]}, send_summary=True)
        assert result["stats"]["generated"] == 1
        assert send.call_count == int(level == "standard")
        original = db.get_last_task_run("strm_sync")
        assert original is not None
        coordinator = refresh.MediaRefreshCoordinator()
        group = claim_due_media_refreshes(owner=coordinator._owner, force=True)[0]
        client = Mock(display_name="Jellyfin")
        client.refresh_for_paths.return_value = {"ok": False, "retryable": True, "fallback": "HTTP 503"}
        with patch.object(coordinator, "_client_for", return_value=client):
            coordinator._process_group(group)
        db.init_db()
        row = db.get_task_run(original["id"])
        assert row is not None
        payload = json.loads(row["result"])
        assert row["status"] == "success"
        assert payload["media_refresh"] == {"Jellyfin": "failed"}
        assert payload["media_refresh_results"]["status"] == "failed"
        assert "HTTP 503" in str(payload["media_refresh_results"])
        public = scheduler_module.strm_status(original["id"])
        assert public["status"] == "completed"
        assert public["data"]["task"]["media_refresh"] == {"status": "failed", "pending": False, "providers": {"Jellyfin": "failed"}}
        assert "HTTP 503" not in str(public)
        assert list(tmp_path.rglob("*.strm"))
        assert claim_due_media_refreshes(owner="restart", force=True) == []

        scheduler_module.reconcile_strm_media_refresh_results()
        notification_center.drain_telegram_notifications()
        STRMScheduler._notify_success(
            result["stats"], {"Jellyfin": "queued"}, 1.0, "manual", [], str(tmp_path),
            run_id=original["id"], chat_ids=[saved_chat],
        )
        assert send.call_count == 1
        assert edit.call_count == int(level == "standard")
        call = edit.call_args if level == "standard" else send.call_args
        assert call.kwargs["chat_id"] == saved_chat
        if level == "standard":
            assert call.kwargs["message_id"] == 73
            assert dict(call.args[0].fields)["STRM 变化"] == dict(send.call_args.args[0].fields)["STRM 变化"]
        assert dict(call.args[0].fields)["媒体库刷新"] == "Jellyfin 失败"
        assert "手动刷新" in call.args[0].footer


@pytest.mark.parametrize("probe", [False, True])
@pytest.mark.parametrize("notify_override", [False, True])
def test_silent_confirmation_receives_refresh_terminal_result(tmp_path, probe, notify_override):
    from app.repositories import organize_confirmations as confirmations

    with isolated_test_database(), _providers("jellyfin"), patch(
        "app.modules.telegram_notification_center.get_notification_thread_event",
        side_effect=AssertionError("静默确认不能读取或投递 TG 线程"),
    ) as telegram_thread:
        confirmations.create_organize_confirmation(
            token="s06-silent", fingerprint="s06-silent", chat_id="s06-chat",
            source_name="S", directory_path="Film", payload={}, expires_at="2099-01-01 00:00:00",
        )
        confirmations.update_organize_confirmation("s06-silent", status="completed", result_json="{}")
        refs = [{
            "topic": "confirmation", "thread_key": "confirmation:s06-silent", "token": "s06-silent",
            "chat_id": "s06-chat", "topic_enabled": False,
        }]
        options = {"notify_override": notify_override, "notification_threads": refs}
        if probe:
            options = {"notification_scopes": [{"probe": True, "notify_override": notify_override,
                "context": {"notification_threads": refs, "notify_enabled": True, "topic_enabled": True}}]}
        _run(tmp_path, run_options=options)
        worker = refresh.MediaRefreshCoordinator()
        group = claim_due_media_refreshes(owner=worker._owner, force=True)[0]
        client = Mock(display_name="Jellyfin")
        client.refresh_for_paths.return_value = {"ok": False, "fallback": "HTTP 503"}
        with patch.object(worker, "_client_for", return_value=client):
            worker._process_group(group)
        row = confirmations.get_organize_confirmation("s06-silent")
        assert row is not None
        assert row["status"] == "completed"
        downstream = json.loads(row["result_json"])["_downstream"]
        assert downstream["strm"] == "完成"
        assert "失败" in downstream["media_refresh"]
        assert downstream["partial"]
        telegram_thread.assert_not_called()


def test_multiple_providers_preserve_failure_and_deferred_success_after_restart(tmp_path):
    with isolated_test_database(), _providers("jellyfin", "emby"):
        _run(tmp_path)
        row = db.get_last_task_run("strm_sync")
        assert row is not None
        run_id = row["id"]
        worker = refresh.MediaRefreshCoordinator()
        groups = {group["provider"]: group for group in claim_due_media_refreshes(owner=worker._owner, force=True)}
        path = groups["jellyfin"]["paths"][0]
        client = Mock(display_name="fixture")
        client.refresh_for_paths.return_value = {"ok": False, "fallback": "HTTP 503"}
        with patch.object(worker, "_client_for", return_value=client):
            worker._process_group(groups["emby"])
        client.refresh_for_paths.return_value = {"ok": True, "deferred_targets": {path: "film"}}
        with patch.object(worker, "_client_for", return_value=client):
            worker._process_group(groups["jellyfin"])
        row = db.get_task_run(run_id)
        assert row is not None
        result = json.loads(row["result"])
        assert result["media_refresh_results"]["status"] == "partial"
        assert result["refresh_pending"]
        db.init_db()
        worker = refresh.MediaRefreshCoordinator()
        pending = claim_due_media_refreshes(owner=worker._owner, force=True)
        assert len(pending) == 1 and pending[0]["provider"] == "jellyfin"
        client.refresh_for_paths.return_value = {"ok": True, "succeeded_paths": [path]}
        with patch.object(worker, "_client_for", return_value=client):
            worker._process_group(pending[0])
        row = db.get_task_run(run_id)
        assert row is not None
        result = json.loads(row["result"])
        assert result["media_refresh"] == {"Emby": "failed", "Jellyfin": "success"}
        assert not result["refresh_pending"]
        assert row["status"] == "success"
        assert claim_due_media_refreshes(owner="later", force=True) == []
        assert json.loads(db.kv_get("media_refresh_queue:v1"))["events"] == {}


def test_projection_outage_restarts_without_replaying_actual_refresh(tmp_path):
    with isolated_test_database(), _providers("jellyfin"):
        _run(tmp_path)
        row = db.get_last_task_run("strm_sync")
        assert row is not None
        worker = refresh.MediaRefreshCoordinator()
        group = claim_due_media_refreshes(owner=worker._owner, force=True)[0]
        client = Mock(display_name="fixture")
        client.refresh_for_paths.return_value = {"ok": False, "fallback": "HTTP 503"}
        with patch.object(worker, "_client_for", return_value=client), patch.object(
            scheduler_module, "_publish_linked_notification_threads", side_effect=sqlite3.OperationalError("outbox unavailable"),
        ):
            worker._process_group(group)
        state = json.loads(db.kv_get("media_refresh_queue:v1"))
        assert state["events"] and state["origins"] and not state["groups"]
        db.init_db()
        restarted = refresh.MediaRefreshCoordinator()
        with patch.object(restarted, "_client_for") as retry:
            restarted._reconcile_results()
        retry.assert_not_called()
        assert client.refresh_for_paths.call_count == 1
        state = json.loads(db.kv_get("media_refresh_queue:v1"))
        assert not state["events"] and not state["origins"]
        result = db.get_task_run(row["id"])
        assert result is not None
        assert json.loads(result["result"])["media_refresh"] == {"Jellyfin": "failed"}


def test_actual_client_partial_results_are_saved_on_original_run(tmp_path):
    from tests.test_deep_audit_media_chain import MediaEndpoint

    tree = {"s": [GuangYaFile("a", "A", True, parent_id="s"), GuangYaFile("b", "B", True, parent_id="s")],
            "a": [GuangYaFile("va", "A.mkv", False, 100, "a", "a")],
            "b": [GuangYaFile("vb", "B.mkv", False, 100, "b", "b")]}
    with isolated_test_database(), _providers("jellyfin"):
        _run(tmp_path, tree=tree)
        endpoint = MediaEndpoint(tmp_path / strm.STRM_SUBDIR, series={
            "accepted-a": tmp_path / strm.STRM_SUBDIR / "A", "failed-b": tmp_path / strm.STRM_SUBDIR / "B",
        })
        post = endpoint.post

        def partial_http(url, **kwargs):
            if "failed-b" in url:
                raise OSError("fixture HTTP 503")
            return post(url, **kwargs)

        worker = refresh.MediaRefreshCoordinator()
        group = claim_due_media_refreshes(owner=worker._owner, force=True)[0]
        with patch.object(worker, "_client_for", side_effect=endpoint.client), patch.object(endpoint, "post", side_effect=partial_http):
            worker._process_group(group)
        row = db.get_last_task_run("strm_sync")
        assert row is not None and row["status"] == "success"
        result = json.loads(row["result"])["media_refresh_results"]
        assert result["status"] == "partial" and not result["pending"]
        statuses = {Path(event["path"]).parent.name: event["providers"]["jellyfin"]["status"] for event in result["events"].values()}
        assert statuses == {"A": "success", "B": "failed"}
        assert endpoint.posts == ["/Items/accepted-a/Refresh"]
        assert claim_due_media_refreshes(owner="restart", force=True) == []


@pytest.mark.parametrize("remaining", [("jellyfin",), ()])
def test_provider_disabled_before_failed_enqueue_recovery_reaches_terminal(tmp_path, remaining):
    enqueue = refresh.enqueue_media_refresh

    def unavailable(provider, *args, **kwargs):
        if provider == "emby":
            raise sqlite3.OperationalError("fixture unavailable")
        return enqueue(provider, *args, **kwargs)

    with isolated_test_database():
        with _providers("jellyfin", "emby"), patch.object(refresh, "enqueue_media_refresh", side_effect=unavailable):
            _run(tmp_path)
        assert repository.count_strm_refresh_paths() == 1
        client = Mock(display_name="fixture")
        client.refresh_for_paths.return_value = {"ok": True}
        with _providers(*remaining), patch("app.clients.jellyfin.JellyfinClient", return_value=client):
            STRMMetadataWorker()._flush_media_refresh(force=True)
            assert repository.count_strm_refresh_paths() == 0
            worker = refresh.MediaRefreshCoordinator()
            groups = claim_due_media_refreshes(owner=worker._owner, force=True)
            assert {group["provider"] for group in groups} == {"jellyfin", "emby"}
            for group in groups:
                worker._process_group(group)
        assert client.refresh_for_paths.call_count == int("jellyfin" in remaining)
        db.init_db()
        worker._reconcile_results()
        row = db.get_last_task_run("strm_sync")
        assert row is not None
        result = json.loads(row["result"])
        assert result["media_refresh"] == {"Jellyfin": "success" if remaining else "failed", "Emby": "failed"}
        assert row["status"] == "success" and result["strm_partial"] is False
        assert not result["refresh_pending"]
        state = json.loads(db.kv_get("media_refresh_queue:v1"))
        assert state["events"] == state["origins"] == state["groups"] == {}


def test_faster_refresh_cannot_be_overwritten_by_scheduler_queued_result(tmp_path):
    with isolated_test_database(), _providers("jellyfin"):
        enqueue = refresh.enqueue_media_refresh_paths
        worker = refresh.MediaRefreshCoordinator()
        client = Mock(display_name="fixture")
        client.refresh_for_paths.return_value = {"ok": False, "fallback": "HTTP 503"}

        def consume_before_return(*args, **kwargs):
            result = enqueue(*args, **kwargs)
            group = claim_due_media_refreshes(owner=worker._owner, force=True)[0]
            with patch.object(worker, "_client_for", return_value=client):
                worker._process_group(group)
            return result

        with patch.object(refresh, "enqueue_media_refresh_paths", side_effect=consume_before_return):
            _run(tmp_path)
        row = db.get_last_task_run("strm_sync")
        assert row is not None and row["status"] == "success"
        assert json.loads(row["result"])["media_refresh"] == {"Jellyfin": "failed"}


@pytest.mark.parametrize("newer_finished", [False, True])
def test_run_fence_preserves_newer_confirmation_after_origin_gc(tmp_path, newer_finished):
    from app.repositories import organize_confirmations as confirmations

    with isolated_test_database():
        confirmations.create_organize_confirmation(
            token="s06-newer", fingerprint="s06-newer", chat_id="s06-chat", source_name="S",
            directory_path="Film", payload={"_notification_suppressed": True}, expires_at="2099-01-01 00:00:00",
        )
        confirmations.update_organize_confirmation("s06-newer", status="completed", result_json="{}")
        options = {"notify_override": False, "notification_threads": [{
            "topic": "confirmation", "thread_key": "confirmation:s06-newer", "token": "s06-newer",
            "chat_id": "s06-chat", "topic_enabled": False,
        }]}
        with _providers("jellyfin"):
            _run(tmp_path / "old", run_options=options)
        old_run = db.get_last_task_run("strm_sync")
        assert old_run is not None
        worker = refresh.MediaRefreshCoordinator()
        old = claim_due_media_refreshes(owner=worker._owner, force=True)[0]
        with _providers("emby"):
            _run(tmp_path / "new", run_options=options)
        new_run = db.get_last_task_run("strm_sync")
        assert new_run is not None
        new = claim_due_media_refreshes(owner=worker._owner, force=True)[0]
        client = Mock(display_name="fixture")
        client.refresh_for_paths.return_value = {"ok": True}
        if newer_finished:
            with patch.object(worker, "_client_for", return_value=client):
                worker._process_group(new)
            assert str(new_run["id"]) not in json.loads(db.kv_get("media_refresh_queue:v1"))["origins"]
        row = confirmations.get_organize_confirmation("s06-newer")
        assert row is not None
        newer = json.loads(row["result_json"])["_downstream"]
        assert newer["media_refresh"] == ("Emby 请求已受理" if newer_finished else "Emby 已排队")
        client.refresh_for_paths.return_value = {"ok": False, "fallback": "late HTTP 503"}
        with patch.object(worker, "_client_for", return_value=client):
            worker._process_group(old)
        row = confirmations.get_organize_confirmation("s06-newer")
        assert row is not None
        assert json.loads(row["result_json"])["_downstream"] == newer
        old_history = db.get_task_run(old_run["id"])
        new_history = db.get_task_run(new_run["id"])
        assert old_history is not None and new_history is not None
        assert json.loads(old_history["result"])["media_refresh"] == {"Jellyfin": "failed"}
        assert json.loads(new_history["result"])["media_refresh"] == {"Emby": "success" if newer_finished else "queued"}
        if not newer_finished:
            client.refresh_for_paths.return_value = {"ok": True}
            with patch.object(worker, "_client_for", return_value=client):
                worker._process_group(new)
        db.init_db()
        worker._reconcile_results()
        state = json.loads(db.kv_get("media_refresh_queue:v1"))
        assert state["events"] == state["origins"] == state["groups"] == {}


@pytest.mark.parametrize("trigger_type", ["organize", "manual", "cron"])
def test_organize_refresh_off_survives_recovery_and_metadata_without_affecting_independent_sync(tmp_path, trigger_type):
    from tests.test_strm_metadata_root_policy import _Response

    tree = _tree()
    tree["d"].append(GuangYaFile("nfo", "Movie.nfo", False, 8, "n1", "d"))
    with isolated_test_database(), _providers("jellyfin", "emby"):
        get_bool = refresh.config.get_bool
        with patch.object(scheduler_module, "get_bool", side_effect=lambda key, default=False: (
            False if key == "GY_ORGANIZE_EMBY_REFRESH" else get_bool(key, default)
        )), patch.object(refresh, "enqueue_media_refresh_paths", return_value={"Jellyfin": "pending", "Emby": "pending"}):
            # 模拟进程重启后的持久变化领取：没有原始内存 override。
            repository.enqueue_strm_change_targets([{
                "source_id": "s", "kind": "video", "action": "upsert", "file_id": "v",
                "parent_id": "d", "name": "Movie.mkv", "rel_dir": "Film",
            }])
            result = _run(tmp_path, enabled=None, tree=tree, trigger_type=trigger_type, metadata_exts={"nfo"})
        assert result["ok"] and result["stats"]["generated"] == 1
        jobs = repository.list_strm_metadata_queue()
        assert len(jobs) == 1
        enabled = trigger_type != "organize"
        assert json.loads(jobs[0]["refresh_policy_json"])["enabled"] is enabled
        entries = repository.list_strm_refresh_entries()
        assert bool(entries) is enabled
        assert all(entry["allow_emby"] for entry in entries)
        repository.acknowledge_strm_refresh_paths(entries)
        worker = STRMMetadataWorker()
        worker._client = cast(GuangYaClient, _TreeClient(tree))
        with patch("app.modules.strm_metadata_worker.get_bool", return_value=True), patch(
            "app.modules.strm_metadata_worker.get", side_effect=lambda key, default="": (
                str(tmp_path) if key == "STRM_ROOT" else "nfo" if key == "STRM_METADATA_EXTS" else default
            ),
        ), patch("app.modules.strm.requests.get", return_value=_Response()), patch.object(
            refresh, "enqueue_media_refresh_paths", return_value={"Jellyfin": "pending", "Emby": "pending"},
        ):
            assert worker._process_one()
        assert (tmp_path / strm.STRM_SUBDIR / "Film" / "Movie.nfo").is_file()
        entries = repository.list_strm_refresh_entries()
        assert bool(entries) is enabled
        assert all(entry["allow_emby"] for entry in entries)
        if enabled:
            _consume(providers=("jellyfin", "emby"), expected_paths=[entry["path"] for entry in entries])
        else:
            worker._flush_media_refresh(force=True)
            assert media_refresh_queue_status()["paths"] == 0


@pytest.mark.parametrize("switches", [(False, True), (True, False)])
def test_coalesced_organize_requests_never_reenable_a_disabled_batch(tmp_path, switches):
    with isolated_test_database(), patch.object(scheduler_module.threading, "Thread"):
        scheduler = STRMScheduler()
        for index, enabled in enumerate(switches):
            assert scheduler.trigger(
                "organize", media_server_refresh_override=enabled, debounce_seconds=1,
                organize_changes=[{"source_id": "s", "file_id": str(index), "kind": "video", "action": "upsert"}],
            )["ok"]
        options = scheduler._pending_organize_options
        assert options is not None and len(options["organize_changes"]) == 2
        scheduler._run_options = options
        policy = scheduler._refresh_overflow_sink(str(tmp_path))
        path = Path(policy.media_root) / "Film" / "Movie.strm"
        repository.upsert_strm_index("guangya:s", "v", "e", 1, "Movie.mkv", str(path), refresh_policy=policy)
        policy([str(path.parent)])
        assert repository.list_strm_refresh_entries() == []


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
from app.repositories import strm as repository
from tests.test_strm_refresh_closure import _run, _providers

db.configure_database(Path(sys.argv[1]), test_mode=True)
original = repository.upsert_strm_index

def crash_after_commit(*args, **kwargs):
    original(*args, **kwargs)
    os._exit(73)

with _providers('jellyfin', 'emby'), patch.object(repository, 'upsert_strm_index', side_effect=crash_after_commit):
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
        assert len(paths) == len(repository.list_strm_index("guangya:s")) == 1
        entries = repository.list_strm_refresh_entries()
        assert len(entries) == int(enabled)
        if entries:
            assert entries[0]["allow_emby"] is allow_emby
        before = paths[0].read_bytes()
        replay = _run(tmp_path, enabled=enabled, allow_emby=allow_emby)
        assert replay["stats"]["generated"] == 0 and replay["stats"]["skipped"] == 1
        assert paths[0].read_bytes() == before
        assert repository.list_strm_refresh_entries() == entries
        if enabled:
            _consume(providers=("jellyfin", "emby") if allow_emby else ("jellyfin",), expected_paths=[str(paths[0])])
        else:
            with _providers("jellyfin", "emby"):
                STRMMetadataWorker()._flush_media_refresh(force=True)
            assert media_refresh_queue_status()["paths"] == 0


@pytest.mark.parametrize("origin", ["outbox", "foreground", "task"])
def test_empty_provider_finishes_without_pending_refresh_or_later_replay(tmp_path, origin):
    target = str(tmp_path / strm.STRM_SUBDIR / "Film")
    with isolated_test_database(), _providers():
        if origin == "outbox":
            repository.enqueue_strm_refresh_paths([target], allow_emby=False)
            worker = STRMMetadataWorker()
            worker._flush_media_refresh(force=True)
            assert not worker._refresh_retry_pending
        elif origin == "foreground":
            with patch.object(scheduler_module, "get", side_effect=lambda k, d="": str(tmp_path) if k == "STRM_ROOT" else d):
                result = STRMScheduler._refresh_media_servers(
                    changed_dirs=[target], refresh_policy=repository.StrmRefreshPolicy(allow_emby=False),
                )
            assert result == {}
        else:
            result = _run(tmp_path)
            assert result["stats"]["generated"] == 1
            assert result["ok"] and not result["partial"]
            assert not result["refresh_pending"] and result["media_refresh"] == {}
            last_run = db.get_last_task_run("strm_sync")
            assert last_run is not None
            assert last_run["status"] == "success"
        assert repository.count_strm_refresh_paths() == 0
        assert media_refresh_queue_status()["paths"] == 0
        with _providers("jellyfin"):
            STRMMetadataWorker()._flush_media_refresh(force=True)
        assert claim_due_media_refreshes(owner="later", force=True) == []


def test_full_scan_during_cloud_move_preserves_last_index_until_stable_scan(tmp_path):
    video = GuangYaFile("v", "Movie.mkv", False, 100, "e", "old")
    tree = {
        "s": [GuangYaFile("old", "Z", True, parent_id="s"), GuangYaFile("new", "A", True, parent_id="s")],
        "old": [video], "new": [],
    }
    media_root = tmp_path / strm.STRM_SUBDIR
    old, new = (media_root / directory / "Movie.strm" for directory in ("Z", "A"))

    def move_after_listing(client, directory):
        listing = client.tree[directory]
        if directory == "old":
            client.tree["old"] = []
            client.tree["new"] = [replace(video, parent_id="new")]
        return listing

    with isolated_test_database(), _providers("jellyfin"), patch.dict(os.environ, STRM_SCAN_WORKERS="1"):
        initial = _run(tmp_path, tree=tree)
        assert initial["stats"]["generated"] == 1
        _consume(providers=("jellyfin",), expected_paths=[str(old)])
        pointer = old.read_bytes()

        with patch.object(_TreeClient, "list_dir", move_after_listing):
            interrupted = _run(tmp_path, tree=tree)
        run = db.get_last_task_run("strm_sync")
        assert run is not None
        assert (run["status"], interrupted["stats"]["generated"], interrupted["stats"]["cleaned"]) == (
            "partial", 0, 0,
        )
        source_stats = interrupted["sources"][0]["stats"]
        assert source_stats["scan_incomplete"] and interrupted["stats"]["clean_skipped"]
        assert any("同一文件出现在两个云端目录" in sample for sample in source_stats["error_samples"])
        assert old.read_bytes() == pointer and not new.exists()
        assert [row["strm_path"] for row in repository.list_strm_index("guangya:s")] == [str(old)]
        assert repository.count_strm_refresh_paths() == 0 and media_refresh_queue_status()["paths"] == 0

        recovered = _run(tmp_path, tree=tree)
        assert recovered["ok"] and not recovered["partial"]
        assert not old.exists() and new.read_bytes() == pointer
        assert [row["strm_path"] for row in repository.list_strm_index("guangya:s")] == [str(new)]
        _consume(providers=("jellyfin",), expected_paths=[str(old), str(new)])


@pytest.mark.parametrize("removal", ["cloud-delete", "retirement"])
def test_single_source_root_file_create_delete_and_refresh(tmp_path, removal):
    with isolated_test_database(), _providers("jellyfin"):
        created = _run(tmp_path, flat=True, allow_emby=False)
        path = tmp_path / strm.STRM_SUBDIR / "Movie.strm"
        assert created["stats"]["generated"] == 1 and path.is_file()
        _consume(providers=("jellyfin",), expected_paths=[str(path)])
        if removal == "retirement":
            db.enqueue_strm_retired_source("s", "S", str(tmp_path))
        deleted = _run(tmp_path, flat=True, allow_emby=False, tree={"s": []}, retire=removal == "retirement")
        assert deleted["stats"]["cleaned"] == 1
        assert not path.exists() and not repository.list_strm_index("guangya:s")
        assert not db.list_strm_retired_sources()
        _consume(providers=("jellyfin",), expected_paths=[str(path)])


@pytest.mark.parametrize("existing", [False, True])
def test_failed_file_index_outbox_commit_retries_instead_of_becoming_current(tmp_path, existing):
    with isolated_test_database(), _providers("jellyfin"):
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
        assert len(repository.list_strm_index("guangya:s")) == int(existing)
        assert (path.read_bytes() if path.exists() else None) == before
        assert repository.count_strm_refresh_paths() == 0
        replay = _run(tmp_path, allow_emby=False)
        assert replay["stats"]["generated"] == 1 and replay["stats"]["skipped"] == 0
        assert repository.list_strm_index("guangya:s")[0]["etag"] == "e"
        _consume(providers=("jellyfin",), expected_paths=[str(path)])


def test_failed_delete_restores_file_and_index_until_refresh_is_durable(tmp_path):
    with isolated_test_database(), _providers("jellyfin"):
        _run(tmp_path, enabled=False)
        with patch.object(repository, "_enqueue_strm_refresh_paths", side_effect=sqlite3.OperationalError("outbox unavailable")):
            _run(tmp_path, tree={"s": []})
        assert len(list(tmp_path.rglob("*.strm"))) == 1
        assert len(repository.list_strm_index("guangya:s")) == 1
        assert repository.count_strm_refresh_paths() == 0
        replay = _run(tmp_path, tree={"s": []}, allow_emby=False)
        assert replay["stats"]["cleaned"] == 1
        assert not repository.list_strm_index("guangya:s")
        _consume(providers=("jellyfin",), expected_paths=[str(tmp_path / strm.STRM_SUBDIR / "Film" / "Movie.strm")])


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
        assert len(repository.list_strm_index("guangya:s")) == 1
        assert len(db.list_strm_retired_sources()) == 1


@pytest.mark.parametrize("retire", [False, True])
def test_delete_commit_crash_retains_refresh_after_index_disappears(tmp_path, retire):
    child_code = _CHILD.replace(
        "original = repository.upsert_strm_index", "original = repository.delete_strm_index_ids"
    ).replace(
        "patch.object(repository, 'upsert_strm_index'", "patch.object(repository, 'delete_strm_index_ids'"
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
        assert not repository.list_strm_index("guangya:s")
        assert repository.count_strm_refresh_paths() == 1
        replay = _run(tmp_path, tree={"s": []}, retire=retire, allow_emby=False)
        assert replay["stats"]["generated"] == replay["stats"]["cleaned"] == 0
        assert not db.list_strm_retired_sources()
        _consume(providers=("jellyfin",), expected_paths=[str(tmp_path / strm.STRM_SUBDIR / "Film" / "Movie.strm")])


def test_move_from_root_keeps_old_file_refresh_until_recovery_handoff(tmp_path):
    with isolated_test_database(), _providers("jellyfin"):
        _run(tmp_path, flat=True, enabled=False)
        previous = tmp_path / strm.STRM_SUBDIR / "Movie.strm"
        result = _run(tmp_path, allow_emby=False)
        assert result["stats"]["generated"] == result["stats"]["cleaned"] == 1
        assert not previous.exists()
        assert not repository.list_strm_path_cleanup("guangya:s")
        _consume(providers=("jellyfin",), expected_paths=[str(previous), str(previous.parent / "Film" / "Movie.strm")])


def test_skipped_provider_is_not_a_durable_handoff(tmp_path):
    with isolated_test_database():
        repository.enqueue_strm_refresh_paths([str(tmp_path / "Film")], allow_emby=False)
        with patch.object(refresh, "enqueue_media_refresh_paths", return_value={"Jellyfin": "skipped"}):
            STRMMetadataWorker()._flush_media_refresh(force=True)
        assert repository.count_strm_refresh_paths() == 1
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
            assert not repository.list_strm_index("guangya:s") and not db.list_strm_retired_sources()
            assert not list(tmp_path.rglob("*.strm"))
            assert repository.count_strm_refresh_paths() == 0
            assert run["status"] == "success" and result["media_refresh"] == {}
        finally:
            assert scheduler.stop(timeout=5)
        with _providers("jellyfin"):
            STRMMetadataWorker()._flush_media_refresh(force=True)
        assert claim_due_media_refreshes(owner="later", force=True) == []


def test_commit_and_overflow_share_frozen_policy_and_do_not_duplicate_paths(tmp_path):
    with isolated_test_database():
        scheduler = STRMScheduler()
        scheduler._run_options = {"media_server_refresh_override": True}
        first = replace(scheduler._refresh_overflow_sink(str(tmp_path / "first")), allow_emby=False)
        path = Path(first.media_root) / "Film" / "Movie.strm"
        repository.upsert_strm_index("guangya:s", "v", "e", 1, "Movie.mkv", str(path), refresh_policy=first)
        first([str(path.parent)])
        assert repository.count_strm_refresh_paths() == 2
        scheduler._run_options = {"media_server_refresh_override": False}
        disabled = scheduler._refresh_overflow_sink(str(tmp_path / "second"))
        disabled([str(Path(disabled.media_root) / "Film")])
        # 后一轮开关/根改变，不能污染上一轮已捕获策略；同一路径重复交接不产生副本。
        first([str(path.parent)])
        assert {(entry["path"], entry["allow_emby"]) for entry in repository.list_strm_refresh_entries()} == {
            (str(path), False), (str(path.parent), False),
        }
        scheduler._run_options = {"media_server_refresh_override": True}
        second = scheduler._refresh_overflow_sink(str(tmp_path / "second"))
        second([str(Path(second.media_root) / "Film")])
        assert {(entry["path"], entry["allow_emby"]) for entry in repository.list_strm_refresh_entries()} == {
            (str(path), False), (str(path.parent), False), (str(Path(second.media_root) / "Film"), True),
        }


def test_notification_pending_refresh_label_is_localized_and_not_failed():
    label = scheduler_module._refresh_notification_label({"媒体库": "pending", "Emby": "queued"})
    assert label == "媒体库 待重试（等待配置恢复） / Emby 已排队"


@pytest.mark.parametrize("success", [True, "success"])
def test_strm_summary_reports_accepted_refresh_and_partial_provider(success):
    event = STRMScheduler._build_success_event(
        {"generated": 1}, {"Jellyfin": success, "Emby": "partial"}, 1.0,
        "manual", [{"id": "s", "name": "S"}], "/synthetic/strm",
    )
    assert event.title == "⚠️ STRM 同步部分完成"
    assert dict(event.fields)["媒体库刷新"] == "Jellyfin 请求已受理 / Emby 部分失败（请手动刷新）"


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
    with isolated_test_database(), patch.object(
        refresh, "enqueue_media_refresh_paths", return_value={"媒体库": "pending"},
    ):
        result = _run(tmp_path, allow_emby=False)
        assert result["partial"] and not result["strm_partial"]
        assert result["media_refresh"] == {"媒体库": "pending"}
        recipients = ["100", "200"] if restricted else ["100"]
        with patch("app.modules.telegram_notification_center.publish_notification_thread") as publish:
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
