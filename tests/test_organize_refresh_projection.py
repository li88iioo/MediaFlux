"""整理刷新恢复的持久投影边界；通知与刷新仓储真实执行，Telegram 离线。"""
from __future__ import annotations

import json
import socket
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import database as db
from app.modules import telegram_notification_center as center
from app.modules import telegram_notification_policy as policy
from app.modules import telegram_organize_lifecycle as lifecycle
from app.modules.organize_probe_notifications import (
    build_notification_context,
    publish_probe_acknowledged,
)
from app.notifier import NotificationEvent, TelegramSendResult
from app.repositories.telegram_notifications import get_notification
from tests.support import isolated_test_database

TASK = "refresh-parent"
CHAT = "100"
PATHS = ("/isolated-strm/series/01.strm", "/isolated-strm/series/02.strm")
QUEUED = "已排队"


@pytest.fixture
def transport(monkeypatch):
    def offline(*_args, **_kwargs):
        raise AssertionError("刷新投影测试禁止外联")

    monkeypatch.setattr(socket.socket, "connect", offline)
    monkeypatch.setattr(socket, "create_connection", offline)
    monkeypatch.setattr(policy, "notifications_enabled", lambda: True)
    monkeypatch.setattr(policy, "notification_level", lambda: "standard")
    monkeypatch.setattr(center, "notification_target_chat_id", lambda chat="": chat or CHAT)
    monkeypatch.setattr(center, "_dispatch_stop", threading.Event())
    monkeypatch.setattr(center, "wake_telegram_notification_dispatcher", Mock())
    send = Mock(return_value=TelegramSendResult(True, message_id=501))
    edit = Mock(return_value=TelegramSendResult(True, message_id=501))
    monkeypatch.setattr(center, "send_event_result", send)
    monkeypatch.setattr(center, "edit_event_result", edit)
    with isolated_test_database("organize-refresh-projection.db") as path:
        yield SimpleNamespace(path=path, send=send, edit=edit)


def _row(task=TASK, chat=CHAT, *, topic="organize"):
    row = get_notification(center.notification_thread_event_key(
        f"organize:{task}", topic=topic, chat_id=chat,
    ))
    assert row is not None
    return row


def _event(task=TASK, chat=CHAT):
    return center.deserialize_notification_event(_row(task, chat)["event_json"])


def _parent(stats=None, *, task=TASK, chat=CHAT, strm_status="已排队"):
    assert lifecycle.publish_organize_lifecycle(
        task, {"total": 1, "moved": 1, **(stats or {})},
        source_name="隔离来源", chat_id=chat, strm_status=strm_status,
    )
    return _event(task, chat)


def _refresh_failure(*, paths=PATHS[:1], allow_emby=True, task=TASK, chat=CHAT):
    entries = db.enqueue_strm_refresh_paths(paths, allow_emby=allow_emby)
    assert lifecycle.update_organize_lifecycle_downstream(
        task, chat_id=chat, strm_status="完成", media_refresh="Jellyfin 失败",
        partial=True, error="媒体库刷新入队失败",
        refresh_handoff={"paths": list(paths), "allow_emby": allow_emby},
    )
    assert "refresh" in _event(task, chat).lifecycle
    return entries


def test_clean_parent_recovers_to_queued_refresh_once(transport):
    original = _parent()
    assert original.state == "queued"
    entries = _refresh_failure()
    assert lifecycle.reconcile_organize_refresh_notifications() == 0
    assert db.acknowledge_strm_refresh_paths(entries) == 1
    assert lifecycle.reconcile_organize_refresh_notifications() == 1
    event = _event()
    assert event.title == "✅ 光鸭整理完成"
    assert event.state == "completed"
    assert dict(event.fields)["媒体库"] == QUEUED
    assert "refresh" not in event.lifecycle
    revision, edits = _row()["revision"], transport.edit.call_count
    assert lifecycle.reconcile_organize_refresh_notifications() == 0
    assert _row()["revision"] == revision
    assert transport.edit.call_count == edits


@pytest.mark.parametrize("stats,title,state,summary", [
    ({"total": 2, "failed": 1}, "⚠️ 光鸭整理部分完成", "partial", "失败 1"),
    ({"total": 2, "need_confirm": 1}, "⏳ 光鸭整理等待人工确认", "partial", "待确认 1"),
    ({"stopped": True}, "⏹️ 光鸭整理已停止", "stopped", "入库 1"),
    ({"scan_complete": False}, "⚠️ 光鸭整理部分完成", "partial", "入库 1"),
    ({"total": 2, "skipped": 1}, "⚠️ 光鸭整理部分完成", "partial", "跳过 1"),
], ids=["failed", "need-confirm", "stopped", "scan-incomplete", "skipped"])
def test_recovery_preserves_parent_business_result(transport, stats, title, state, summary):
    original = _parent(stats)
    entries = _refresh_failure()
    assert db.acknowledge_strm_refresh_paths(entries) == len(entries)
    assert lifecycle.reconcile_organize_refresh_notifications() == 1
    event = _event()
    assert (event.title, event.state, event.footer) == (title, state, original.footer)
    assert summary in str(dict(event.fields)["整理"])
    assert dict(event.fields)["媒体库"] == QUEUED
    assert event.lifecycle["parent"] == original.lifecycle["parent"]
    assert event.lines == original.lines
    assert _row()["importance"] == original.lifecycle["parent"]["importance"]


def test_failed_parent_machine_state_survives_recovery(transport):
    original = _parent({"moved": 0, "failed": 1}, strm_status="未执行")
    assert original.state == "failed"
    entries = _refresh_failure()
    assert db.acknowledge_strm_refresh_paths(entries) == 1
    assert lifecycle.reconcile_organize_refresh_notifications() == 1
    assert _event().state == "failed"
    assert "失败 1" in str(dict(_event().fields)["整理"])


def test_all_exact_paths_and_provider_boundary_control_readiness(transport):
    _parent(task="emby-enabled")
    enabled = _refresh_failure(task="emby-enabled", paths=PATHS)
    _parent(task="emby-disabled")
    disabled = _refresh_failure(task="emby-disabled", paths=PATHS, allow_emby=False)
    db.enqueue_strm_refresh_paths([PATHS[0] + ".other"], allow_emby=True)
    assert db.acknowledge_strm_refresh_paths(enabled[:1]) == 1
    assert lifecycle.reconcile_organize_refresh_notifications() == 0
    assert db.acknowledge_strm_refresh_paths(disabled) == 2
    assert lifecycle.reconcile_organize_refresh_notifications() == 1
    assert _event("emby-disabled").state == "completed"
    assert "refresh" in _event("emby-enabled").lifecycle
    assert db.acknowledge_strm_refresh_paths(enabled[1:]) == 1
    assert lifecycle.reconcile_organize_refresh_notifications() == 1
    assert _event("emby-enabled").state == "completed"
    assert db.count_strm_refresh_paths() == 1  # 无关的相似路径不能阻止精确批次恢复。


def test_recovery_keeps_chat_and_topic_route_when_edit_falls_back(transport):
    entries = {}
    for chat, thread, path in (("100", 500, PATHS[0]), ("200", 600, PATHS[1])):
        _parent({"delivery_route": {"chat_id": chat, "message_thread_id": thread}}, chat=chat)
        entries[chat] = _refresh_failure(chat=chat, paths=[path])
    assert center.publish_notification_thread(
        f"organize:{TASK}", _event(), topic="strm", chat_id=CHAT,
    )
    other_topic = _row(topic="strm")
    other_chat = _row(chat="200")
    transport.edit.return_value = TelegramSendResult(
        False, status_code=400, error="Bad Request: message to edit not found",
    )
    transport.send.reset_mock()
    assert db.acknowledge_strm_refresh_paths(entries["100"]) == 1
    assert lifecycle.reconcile_organize_refresh_notifications() == 1
    assert _row(chat="200")["event_json"] == other_chat["event_json"]
    assert _row(topic="strm")["revision"] == other_topic["revision"]
    transport.send.assert_called_once()
    assert transport.send.call_args.kwargs == {"chat_id": "100", "message_thread_id": 500}
    assert db.acknowledge_strm_refresh_paths(entries["200"]) == 1
    assert lifecycle.reconcile_organize_refresh_notifications() == 1
    assert transport.send.call_args.kwargs == {"chat_id": "200", "message_thread_id": 600}
    for chat, thread in (("100", 500), ("200", 600)):
        stored = json.loads(_row(chat=chat)["event_json"])
        assert stored["_delivery_route"] == {"chat_id": chat, "message_thread_id": thread}


@pytest.mark.parametrize("after_read", [False, True], ids=["before-recovery", "after-cas-read"])
def test_confirmation_rollup_survives_refresh_recovery(transport, monkeypatch, after_read):
    stats = {"total": 2, "moved": 1, "need_confirm": 1,
             "notification_actionable_confirmation_files": 1,
             "notification_actionable_confirmation_groups": 1}
    _parent(stats)
    entries = _refresh_failure()
    assert db.acknowledge_strm_refresh_paths(entries) == 1

    def confirm():
        assert lifecycle.update_organize_lifecycle_confirmations(
            TASK, chat_id=CHAT, baseline=lifecycle.build_organize_confirmation_rollup(stats),
            outcomes={"groups": 1, "resolved_files": 1, "moved": 1, "metadata": 1},
        )
        assert "refresh" in _event().lifecycle

    if after_read:
        read_ready = lifecycle.list_recovered_organize_refresh_notifications

        def read_then_confirm():
            rows = read_ready()
            assert len(rows) == 1
            confirm()  # 真实提交新汇总，使已读恢复快照的 revision 过期。
            return rows

        with monkeypatch.context() as patcher:
            patcher.setattr(lifecycle, "list_recovered_organize_refresh_notifications", read_then_confirm)
            assert lifecycle.reconcile_organize_refresh_notifications() == 0
        assert dict(_event().fields)["整理"] == "视频 2 · 入库 2 · 元数据 1"
        assert "refresh" in _event().lifecycle
    else:
        confirm()
    assert lifecycle.reconcile_organize_refresh_notifications() == 1
    event = _event()
    assert (event.title, event.state) == ("✅ 光鸭整理完成", "completed")
    assert dict(event.fields)["整理"] == "视频 2 · 入库 2 · 元数据 1"
    assert dict(event.fields)["人工确认"] == "已处理 1 / 1 组 · 入库 1 个视频"
    assert event.footer == "人工确认已全部完成；任务汇总已更新。"
    assert dict(event.fields)["媒体库"] == QUEUED
    assert "refresh" not in event.lifecycle


def test_stale_downstream_write_cannot_revert_confirmation_or_revive_refresh(transport, monkeypatch):
    stats = {"total": 2, "moved": 1, "need_confirm": 1,
             "notification_actionable_confirmation_files": 1,
             "notification_actionable_confirmation_groups": 1}
    _parent(stats)
    entries = _refresh_failure()
    assert db.acknowledge_strm_refresh_paths(entries) == 1
    project = lifecycle._project_downstream
    interleaved = False

    def project_after_newer_updates(previous, **kwargs):
        nonlocal interleaved
        if not interleaved:
            interleaved = True
            # 普通回调已经读完；先真实提交确认汇总，再 ACK 收口其刷新标记。
            assert lifecycle.update_organize_lifecycle_confirmations(
                TASK, chat_id=CHAT, baseline=lifecycle.build_organize_confirmation_rollup(stats),
                outcomes={"groups": 1, "resolved_files": 1, "moved": 1, "metadata": 1},
            )
            assert lifecycle.reconcile_organize_refresh_notifications() == 1
            assert "refresh" not in _event().lifecycle
        return project(previous, **kwargs)

    with monkeypatch.context() as patcher:
        patcher.setattr(lifecycle, "_project_downstream", project_after_newer_updates)
        # 拒绝过期回调或重读重试均可，但不能覆盖刚提交的新投影。
        lifecycle.update_organize_lifecycle_downstream(
            TASK, chat_id=CHAT, strm_status="完成", media_refresh="Jellyfin 已排队",
        )
    assert interleaved
    event = _event()
    assert (dict(event.fields)["整理"], "refresh" in event.lifecycle) == (
        "视频 2 · 入库 2 · 元数据 1", False,
    )
    assert dict(event.fields)["人工确认"] == "已处理 1 / 1 组 · 入库 1 个视频"
    assert event.state == "completed"


def test_failed_notification_write_can_recover_after_database_reopen(transport, monkeypatch):
    _parent()
    entries = _refresh_failure()
    before = _row()
    assert db.acknowledge_strm_refresh_paths(entries) == 1
    with monkeypatch.context() as patcher:
        patcher.setattr(center, "upsert_notification", Mock(side_effect=sqlite3.OperationalError("写入失败")))
        assert lifecycle.reconcile_organize_refresh_notifications() == 0
    assert _row()["event_json"] == before["event_json"]
    assert _row()["revision"] == before["revision"]
    db.configure_database(transport.path, test_mode=True)
    db.init_db()
    assert "refresh" in _event().lifecycle
    assert lifecycle.reconcile_organize_refresh_notifications() == 1
    assert _event().state == "completed"
    assert dict(_event().fields)["媒体库"] == QUEUED


def test_unknown_initial_send_is_never_replayed_by_recovery(transport):
    transport.send.return_value = TelegramSendResult(False, status_code=408, error="timeout")
    _parent()
    assert _row()["status"] == "outcome_unknown"
    entries = _refresh_failure()
    assert db.acknowledge_strm_refresh_paths(entries) == 1
    assert lifecycle.reconcile_organize_refresh_notifications() == 1
    row = _row()
    assert row["status"] == "outcome_unknown"
    assert not row["message_id"]
    assert dict(_event().fields)["媒体库"] == QUEUED
    assert not center.drain_telegram_notifications(event_key=row["event_key"])
    transport.send.assert_called_once()
    transport.edit.assert_not_called()


def test_legacy_event_without_lifecycle_keeps_unattributed_warning(transport):
    previous = NotificationEvent(
        "⚠️ 光鸭整理部分完成", fields=(("整理", "视频 1 · 失败 1"),),
        footer="原父失败仍需处理", state="partial",
    )
    assert center.publish_notification_thread(
        f"organize:{TASK}", previous, topic="organize", chat_id=CHAT, importance="error",
    )
    assert not _event().lifecycle
    entries = _refresh_failure()
    assert db.acknowledge_strm_refresh_paths(entries) == 1
    assert lifecycle.reconcile_organize_refresh_notifications() == 1
    event = _event()
    assert (event.title, event.footer, event.state) == (previous.title, previous.footer, previous.state)
    assert dict(event.fields)["整理"] == "视频 1 · 失败 1"


def test_legacy_strm_failure_survives_confirmation_and_later_success(transport):
    stats = {"total": 2, "moved": 1, "need_confirm": 1,
             "notification_actionable_confirmation_files": 1,
             "notification_actionable_confirmation_groups": 1}
    previous = NotificationEvent(
        "⚠️ 光鸭整理部分完成",
        fields=(("整理", "视频 2 · 入库 1 · 待确认 1"), ("STRM", "失败"), ("媒体库", "未触发")),
        footer="前批 STRM 仍有未归因失败", state="partial",
    )
    assert center.publish_notification_thread(
        f"organize:{TASK}", previous, topic="organize", chat_id=CHAT, importance="error",
    )
    assert not _event().lifecycle
    assert lifecycle.update_organize_lifecycle_confirmations(
        TASK, chat_id=CHAT, baseline=lifecycle.build_organize_confirmation_rollup(stats),
        outcomes={"groups": 1, "resolved_files": 1, "moved": 1, "metadata": 1},
    )
    assert _event().state == "partial"
    assert lifecycle.update_organize_lifecycle_downstream(
        TASK, chat_id=CHAT, strm_status="完成", media_refresh="Jellyfin 已排队",
    )
    event = _event()
    assert dict(event.fields)["整理"] == "视频 2 · 入库 2 · 元数据 1"
    assert event.state == "partial"
    assert str(event.title).startswith("⚠️")
    assert _row()["importance"] == "error"


def test_refresh_recovery_preserves_later_non_refresh_failure(transport):
    _parent()
    entries = _refresh_failure()
    assert lifecycle.update_organize_lifecycle_downstream(
        TASK, chat_id=CHAT, strm_status="部分完成", media_refresh="未触发",
        partial=True, error="新的 STRM 同步失败",
    )
    assert "refresh" in _event().lifecycle
    assert db.acknowledge_strm_refresh_paths(entries) == 1
    assert lifecycle.reconcile_organize_refresh_notifications() == 1
    assert "refresh" not in _event().lifecycle
    assert _event().state == "partial"
    assert _event().footer == "新的 STRM 同步失败"
    assert dict(_event().fields)["媒体库"] == QUEUED


def test_later_success_does_not_hide_pending_refresh_failure(transport):
    _parent()
    entries = _refresh_failure()
    assert lifecycle.update_organize_lifecycle_downstream(
        TASK, chat_id=CHAT, strm_status="完成", media_refresh="Jellyfin 已排队",
    )
    assert _event().state == "partial"
    assert "refresh" in _event().lifecycle
    assert lifecycle.reconcile_organize_refresh_notifications() == 0
    assert db.acknowledge_strm_refresh_paths(entries) == 1
    assert lifecycle.reconcile_organize_refresh_notifications() == 1
    assert _event().state == "completed"


def test_later_success_preserves_unresolved_non_refresh_failure(transport):
    _parent()
    assert lifecycle.update_organize_lifecycle_downstream(
        TASK, chat_id=CHAT, strm_status="部分完成", media_refresh="未触发",
        partial=True, error="前批 STRM 仍有失败",
    )
    assert lifecycle.update_organize_lifecycle_downstream(
        TASK, chat_id=CHAT, strm_status="完成", media_refresh="Jellyfin 已排队",
    )
    assert _event().state == "partial"
    assert _event().footer == "前批 STRM 仍有失败"
    assert _row()["importance"] == "error"


@pytest.mark.parametrize("second_paths,second_allow_emby", [
    (PATHS[1:], True), (PATHS[:1], False),
], ids=["different-paths", "same-path-different-provider"])
def test_same_parent_waits_for_all_failed_refresh_batches(transport, second_paths, second_allow_emby):
    _parent()
    first = _refresh_failure()
    second = _refresh_failure(paths=second_paths, allow_emby=second_allow_emby)
    assert db.acknowledge_strm_refresh_paths(first) == 1
    assert lifecycle.reconcile_organize_refresh_notifications() == 0
    assert _event().state == "partial"
    assert db.acknowledge_strm_refresh_paths(second) == 1
    assert lifecycle.reconcile_organize_refresh_notifications() == 1
    assert _event().state == "completed"


@pytest.mark.parametrize("probe_status,strm_status,state", [
    ("queued", "后台规格补全进行中", "queued"),
    ("failed", "后台规格补全需复核", "partial"),
])
def test_refresh_ack_does_not_clear_probe_work(transport, probe_status, strm_status, state):
    _parent()
    entries = _refresh_failure()
    log_id = db.add_organize_log("guangya", "old", "new", "file", "success", "1")
    job_id = db.enqueue_organize_probe_completion(
        log_id, source_id="source", rel_dir="series", rules={},
        notification_context=build_notification_context(task_id=TASK, chat_id=CHAT),
    )
    with db.get_conn() as conn:
        conn.execute("UPDATE organize_probe_queue SET status=? WHERE id=?", (probe_status, job_id))
    assert db.acknowledge_strm_refresh_paths(entries) == 1
    assert lifecycle.reconcile_organize_refresh_notifications() == 1
    event = _event()
    assert event.state == state
    assert dict(event.fields)["STRM"] == strm_status
    assert dict(event.fields)["媒体库"] == QUEUED
    if probe_status == "queued":
        assert not lifecycle.organize_lifecycle_downstream_settled(event)
    else:
        assert event.lifecycle["downstream"]["partial"] is True


def test_completed_probe_leaves_parent_failure_without_staying_queued(transport):
    _parent({"total": 2, "failed": 1})
    entries = _refresh_failure()
    log_id = db.add_organize_log("guangya", "old", "new", "file", "success", "1")
    job_id = db.enqueue_organize_probe_completion(
        log_id, source_id="source", rel_dir="series", rules={},
        notification_context=build_notification_context(task_id=TASK, chat_id=CHAT),
    )
    assert db.acknowledge_strm_refresh_paths(entries) == 1
    assert lifecycle.reconcile_organize_refresh_notifications() == 1
    assert _event().state == "queued"
    assert "失败 1" in str(dict(_event().fields)["整理"])
    with db.get_conn() as conn:
        conn.execute("UPDATE organize_probe_queue SET status='completed' WHERE id=?", (job_id,))
        job = dict(conn.execute("SELECT * FROM organize_probe_queue WHERE id=?", (job_id,)).fetchone())
    publish_probe_acknowledged(job)
    event = _event()
    assert event.state == "partial"
    assert event.title == "⚠️ 光鸭整理部分完成"
    assert dict(event.fields)["STRM"] == "完成"
    assert dict(event.fields)["整理"] == "视频 2 · 入库 1 · 失败 1"
    assert dict(event.fields)["媒体库"] == QUEUED
    assert "refresh" not in event.lifecycle
