"""整理话题地址必须经过持久任务、延迟确认汇总和 STRM 更新继续有效。"""
from __future__ import annotations

import json
import socket
import threading
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app import database as db
from app.bot import handlers
from app.modules import organize_tasks
from app.modules import telegram_notification_center as center
from app.modules.organize import Organizer, OrganizeRules
from app.modules.organize_confirmations import skip_confirmation
from app.modules.organize_results import read_organize_result
from app.modules.scheduler import _publish_foreground_linked_notification_threads
from app.modules.telegram_notification_policy import NotificationTopic
from app.modules.telegram_organize_lifecycle import (
    publish_organize_lifecycle,
    update_organize_lifecycle_downstream,
)
from app.notifier import TelegramSendResult
from app.repositories.telegram_notifications import get_notification
from tests.support import isolated_test_database


class _ImmediateThread:
    def __init__(self, *, target, args=(), **_kwargs):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


class _OfflineOrganizer(Organizer):
    """替换云盘读写；任务、通知投影和持久化全部使用生产实现。"""

    failure = False

    def __init__(self, client=None):
        self.client = client or SimpleNamespace()

    def close(self):
        pass

    def _validate_target_outside_source(self, *_args):
        pass

    def organize(self, *_args, **_kwargs):
        if self.failure:
            raise RuntimeError("isolated source failure")
        return [], {
            "total": 2, "moved": 1, "need_confirm": 1, "failed": 0,
            "confirmation_groups": [{
                "source_dir_id": "source", "source_parent_id": "parent",
                "source_name": "隔离来源", "directory": "待确认", "identity": "测试媒体",
                "files": [{"file_id": "video", "name": "episode.mkv",
                           "parent_id": "parent", "size": 100}],
                "companions": [], "candidates": [],
            }],
        }


@pytest.fixture
def offline(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("R06 forbids outbound connections")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    with isolated_test_database():
        yield


def _outbox(task_id, chat_id):
    return get_notification(center.notification_thread_event_key(
        f"organize:{task_id}", topic=NotificationTopic.ORGANIZE, chat_id=chat_id,
    ))


@pytest.mark.parametrize("chat_id,thread_id,source_thread_id", [
    (100, 500, 500), (100, 500, None), (100, 500, 600),
    (100, None, 500), (100, None, None), (-100, None, None), (-100, 500, 500),
])
def test_task_result_delayed_confirmation_and_strm_keep_source_route(
    offline, chat_id, thread_id, source_thread_id,
):
    manager = organize_tasks.OrganizeTaskManager()
    rules = OrganizeRules(
        target_dir_id="archive", clean_empty=False, link_strm=True,
        notify_enabled=True, library_notify=True, media_info_enabled=False,
    )
    source = SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, type="private" if chat_id > 0 else "supergroup"),
        message_id=42,
    )
    if source_thread_id is not None:
        source.message_thread_id = source_thread_id
    progress = Mock(source_message=source, message_thread_id=thread_id)
    progress.dismiss_source_message.return_value = True
    scheduler = Mock()
    scheduler.trigger.return_value = {"ok": True, "queued": True}
    next_id = 100

    def sent(_event, **_kwargs):
        nonlocal next_id
        next_id += 1
        return TelegramSendResult(True, message_id=next_id)

    with (
        patch.object(organize_tasks, "Organizer", _OfflineOrganizer),
        patch.object(organize_tasks.threading, "Thread", _ImmediateThread),
        patch.object(organize_tasks, "get_organize_manager", return_value=manager),
        patch.object(manager, "_wake_download_tracker"),
        patch("app.modules.download_tracker.resume_relocated_staging", return_value=[]),
        patch.object(OrganizeRules, "from_config", return_value=rules),
        patch("app.modules.organize.get", side_effect=lambda key, default="": {
            "GY_STRM_BASE_URL": "http://isolated.invalid", "STRM_ROOT": "/isolated-strm",
        }.get(key, default)),
        patch("app.modules.scheduler.get_scheduler", return_value=scheduler),
        patch("app.modules.organize_confirmations.wake_recognition_review_dispatcher"),
        patch("app.modules.telegram_notification_policy.notifications_enabled", return_value=True),
        patch("app.modules.telegram_notification_policy.notification_level", return_value="standard"),
        patch.object(center, "notification_target_chat_id", side_effect=lambda chat="": str(chat or chat_id)),
        patch.object(center, "_dispatch_stop", threading.Event()),
        patch.object(center, "wake_telegram_notification_dispatcher"),
        patch.object(center, "send_event_result", side_effect=sent) as sender,
        patch.object(center, "edit_event_result", return_value=TelegramSendResult(
            False, status_code=400, error="Bad Request: message to edit not found",
        )),
    ):
        state = handlers._run_guangya_organize_stage(
            chat_id, [{"id": "source", "name": "隔离来源"}], "archive", progress,
            notify_results=True, progress_title="光鸭整理",
        )
        assert state["status"] == "completed"
        row = _outbox(state["id"], str(chat_id))
        assert row is not None and row["status"] == "sent"
        expected_thread_id = thread_id or source_thread_id
        expected_route = {"chat_id": str(chat_id), "message_thread_id": expected_thread_id} if expected_thread_id else None
        assert json.loads(row["event_json"]).get("_delivery_route") == expected_route
        persisted = db.get_task_run(state["run_id"])
        assert persisted is not None
        result = json.loads(persisted["result"])
        assert result.get("delivery_route") == expected_route
        assert read_organize_result(result).get("delivery_route") == expected_route
        assert state["result"].get("delivery_route") == expected_route
        assert state["stats"].get("delivery_route") == expected_route
        assert sender.call_args_list[0].kwargs.get("message_thread_id") == expected_thread_id
        assert sender.call_args_list[0].kwargs["chat_id"] == str(chat_id)

        # 候选在初始整理之后才处理；真实 SQLite 确认行驱动父汇总更新。
        candidates = db.list_organize_confirmations_for_task(state["id"], chat_id=str(chat_id))
        assert len(candidates) == 1
        previous_revision = row["revision"]
        previous_sends = sender.call_count
        skip_confirmation(candidates[0]["token"], chat_id=str(chat_id))
        row = _outbox(state["id"], str(chat_id))
        assert row["revision"] > previous_revision
        assert row["status"] == "sent"
        assert json.loads(row["event_json"]).get("_delivery_route") == expected_route
        assert sender.call_count > previous_sends
        assert sender.call_args.kwargs.get("message_thread_id") == expected_thread_id

        # 通过生产 STRM 联动更新入口重建通知；旧消息失效时强制重新发送，
        # 避免仅靠 editMessageText 的 message_id 掩盖路由已被丢弃。
        options = deepcopy(scheduler.trigger.call_args.kwargs)
        previous_revision = row["revision"]
        assert _publish_foreground_linked_notification_threads(
            options, strm_status="同步完成", media_refresh="刷新完成",
        )
        row = _outbox(state["id"], str(chat_id))
        assert row["revision"] > previous_revision
        assert row["status"] == "sent"
        assert json.loads(row["event_json"]).get("_delivery_route") == expected_route
        assert sender.call_args.kwargs.get("message_thread_id") == expected_thread_id
        assert sender.call_args.kwargs["chat_id"] == str(chat_id)


@pytest.mark.parametrize("thread_id,source_thread_id", [(500, None), (500, 600), (None, 500), (None, None)])
def test_handoff_prefers_normalized_progress_route(offline, thread_id, source_thread_id):
    source = SimpleNamespace(chat=SimpleNamespace(id=100))
    if source_thread_id is not None:
        source.message_thread_id = source_thread_id
    progress = Mock(source_message=source, message_thread_id=thread_id)
    manager = Mock()
    manager.start.return_value = {"ok": False, "error": "isolated admission stop"}
    with (
        patch.object(organize_tasks, "get_organize_manager", return_value=manager),
        patch.object(OrganizeRules, "from_config", return_value=OrganizeRules(target_dir_id="archive")),
    ):
        handlers._run_guangya_organize_stage(
            100, [{"id": "source", "name": "隔离来源"}], "archive", progress,
            notify_results=True, progress_title="光鸭整理",
        )
    expected = thread_id or source_thread_id
    route = manager.start.call_args.kwargs.get("delivery_route")
    assert route == ({"chat_id": "100", "message_thread_id": expected} if expected else None)


def test_failed_organize_persists_and_delivers_source_route(offline):
    manager = organize_tasks.OrganizeTaskManager()
    rules = OrganizeRules(target_dir_id="archive", clean_empty=False, link_strm=False)
    source = SimpleNamespace(chat=SimpleNamespace(id=100), message_thread_id=500)
    progress = Mock(source_message=source, message_thread_id=500)
    progress.dismiss_source_message.return_value = True
    with (
        patch.object(organize_tasks, "Organizer", _OfflineOrganizer),
        patch.object(_OfflineOrganizer, "failure", True),
        patch.object(organize_tasks.threading, "Thread", _ImmediateThread),
        patch.object(organize_tasks, "get_organize_manager", return_value=manager),
        patch.object(manager, "_wake_download_tracker"),
        patch.object(OrganizeRules, "from_config", return_value=rules),
        patch("app.modules.telegram_notification_policy.notifications_enabled", return_value=True),
        patch.object(center, "notification_target_chat_id", return_value="100"),
        patch.object(center, "_dispatch_stop", threading.Event()),
        patch.object(center, "wake_telegram_notification_dispatcher"),
        patch.object(center, "send_event_result", return_value=TelegramSendResult(True, message_id=101)) as sender,
    ):
        state = handlers._run_guangya_organize_stage(
            100, [{"id": "source", "name": "隔离来源"}], "archive", progress,
            notify_results=True, progress_title="光鸭整理",
        )
    assert state["status"] == "failed"
    expected = {"chat_id": "100", "message_thread_id": 500}
    persisted = db.get_task_run(state["run_id"])
    assert json.loads(persisted["result"]).get("delivery_route") == expected
    assert read_organize_result(json.loads(persisted["result"]))["delivery_route"] == expected
    assert state["result"]["delivery_route"] == expected
    assert sender.call_args.kwargs["message_thread_id"] == 500
    assert json.loads(_outbox(state["id"], "100")["event_json"])["_delivery_route"] == expected


@pytest.mark.parametrize("status", ["pending", "retry_wait", "outcome_unknown"])
def test_queued_parent_update_restores_route_from_sqlite(offline, status):
    dispatch_stop = threading.Event()
    if status == "pending":
        dispatch_stop.set()
    receipts = [TelegramSendResult(True, message_id=101)]
    if status != "pending":
        receipts.insert(0, TelegramSendResult(
            False, status_code=408 if status == "outcome_unknown" else 503,
            error="isolated transport interruption",
        ))
    route = {"chat_id": "100", "message_thread_id": 500}
    with (
        patch("app.modules.telegram_notification_policy.notifications_enabled", return_value=True),
        patch("app.modules.telegram_notification_policy.notification_level", return_value="standard"),
        patch.object(center, "notification_target_chat_id", return_value="100"),
        patch.object(center, "_dispatch_stop", dispatch_stop),
        patch.object(center, "wake_telegram_notification_dispatcher"),
        patch.object(center, "send_event_result", side_effect=receipts) as sender,
    ):
        published = publish_organize_lifecycle(
            "queued-task", {"total": 1, "moved": 1, "delivery_route": route},
            source_name="隔离来源", chat_id="100",
        )
        assert published.status == status
        dispatch_stop.set()
        # 更新入口没有 stats/route 参数，只能从持久 outbox 继承话题地址。
        updated = update_organize_lifecycle_downstream(
            "queued-task", chat_id="100", strm_status="同步完成", media_refresh="刷新完成",
        )
        assert updated.accepted
        row = get_notification(published.event_key)
        assert row["revision"] == 2
        assert json.loads(row["event_json"])["_delivery_route"] == route
        dispatch_stop.clear()
        delivered = center.drain_telegram_notifications(event_key=published.event_key)
        row = get_notification(published.event_key)
        if status == "outcome_unknown":
            assert not delivered
            assert row["status"] == "outcome_unknown"
            assert sender.call_count == 1  # 话题修复不得恢复未知首次发送的盲重试。
        else:
            assert delivered
            assert row["status"] == "sent"
            assert row["delivered_revision"] == row["revision"]
        assert all(call.kwargs == {"chat_id": "100", "message_thread_id": 500}
                   for call in sender.call_args_list)


@pytest.mark.parametrize("start_fails", [False, True])
def test_admission_persists_route_before_worker_or_start_failure(offline, start_fails):
    class PausedThread(_ImmediateThread):
        def start(self):
            if start_fails:
                raise RuntimeError("isolated worker start failure")

    manager = organize_tasks.OrganizeTaskManager()
    route = {"chat_id": "100", "message_thread_id": 500}
    try:
        with (
            patch.object(organize_tasks, "Organizer", _OfflineOrganizer),
            patch.object(organize_tasks.threading, "Thread", PausedThread),
        ):
            accepted = manager.start(
                [{"id": "source", "name": "隔离来源"}],
                OrganizeRules(target_dir_id="archive", clean_empty=False),
                trigger_type="telegram", chat_id="100", delivery_route=route,
            )
        state = manager.task_result(manager._task["id"])
        persisted = db.get_task_run(state["run_id"])
        assert bool(accepted["ok"]) is not start_fails
        assert persisted["status"] == ("failed" if start_fails else "running")
        assert json.loads(persisted["result"])["delivery_route"] == route
        assert read_organize_result(json.loads(persisted["result"]))["delivery_route"] == route
    finally:
        if not start_fails:
            manager._lock.release()  # 本测试不执行已持久化任务，只检查接纳边界。


@pytest.mark.parametrize("route", [
    {"chat_id": "200", "message_thread_id": 500},
    {"chat_id": "100", "message_thread_id": 0},
    {"chat_id": "100", "message_thread_id": True},
    {"chat_id": "100", "message_thread_id": 500, "unexpected": 1},
])
def test_invalid_route_is_rejected_before_cloud_or_task_write(offline, route):
    manager = organize_tasks.OrganizeTaskManager()
    with patch.object(organize_tasks, "Organizer") as organizer:
        result = manager.start(
            [{"id": "source", "name": "隔离来源"}], OrganizeRules(target_dir_id="archive"),
            trigger_type="telegram", chat_id="100", delivery_route=route,
        )
    assert not result["ok"]
    organizer.assert_not_called()
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 0
