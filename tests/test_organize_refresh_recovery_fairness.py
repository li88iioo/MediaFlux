"""刷新恢复的事实落盘与公平推进：真实 SQLite/outbox，Telegram 离线。"""
from __future__ import annotations

import json
import socket

import pytest

from app import database as db
from app.modules import telegram_notification_center as center
from app.modules import telegram_notification_policy as policy
from app.modules import telegram_organize_lifecycle as lifecycle
from app.notifier import TelegramSendResult
from app.repositories import telegram_notifications as repository
from tests.test_organize_refresh_projection import (
    QUEUED,
    _event,
    _parent,
    _refresh_failure,
    _row,
    transport as transport,
)


@pytest.fixture(autouse=True)
def forbid_connect_ex(monkeypatch):
    def offline(*_args, **_kwargs):
        raise AssertionError("刷新恢复公平性测试禁止外联")

    monkeypatch.setattr(socket.socket, "connect_ex", offline)


@pytest.mark.parametrize("blocked_policy", ["essential", "disabled"])
def test_policy_blocked_unknown_send_still_persists_recovery_without_replay(transport, monkeypatch, blocked_policy):
    transport.send.return_value = TelegramSendResult(False, status_code=408, error="timeout")
    route = {"chat_id": "100", "message_thread_id": 701}
    original = _parent({"delivery_route": route})
    entries = _refresh_failure()
    assert db.acknowledge_strm_refresh_paths(entries) == 1
    before = _row()
    assert before["status"] == "outcome_unknown" and not before["message_id"]
    if blocked_policy == "essential":
        monkeypatch.setattr(policy, "notification_level", lambda: "essential")
    else:
        monkeypatch.setattr(policy, "notifications_enabled", lambda: False)

    lifecycle.reconcile_organize_refresh_notifications()
    recovered = _row()
    event = _event()
    assert "refresh" not in event.lifecycle, "发送策略不能阻止已 ACK 的刷新事实在原 outbox 落盘"
    assert dict(event.fields)["媒体库"] == QUEUED
    assert (event.title, event.state, event.footer) == (original.title, "completed", original.footer)
    assert recovered["revision"] == before["revision"] + 1
    assert json.loads(recovered["event_json"])["_delivery_route"] == route
    for key in ("id", "event_key", "thread_key", "chat_id", "status", "message_id",
                "delivered_revision", "attempts", "lease_generation", "last_error"):
        assert recovered[key] == before[key], key

    db.configure_database(transport.path, test_mode=True)
    db.init_db()
    assert _row()["event_json"] == recovered["event_json"]
    monkeypatch.setattr(policy, "notifications_enabled", lambda: True)
    monkeypatch.setattr(policy, "notification_level", lambda: "standard")
    assert lifecycle.reconcile_organize_refresh_notifications() == 0
    assert not center.drain_telegram_notifications(event_key=recovered["event_key"])
    final = _row()
    assert final["status"] == "outcome_unknown" and not final["message_id"]
    assert final["revision"] == recovered["revision"]
    transport.send.assert_called_once()
    transport.edit.assert_not_called()


@pytest.mark.parametrize("target,revision", [("missing", 1), ("refresh-parent", None), ("refresh-parent", 0)])
def test_fact_update_requires_an_existing_thread_and_revision(transport, target, revision):
    previous = _parent()
    before = _row()
    transport.send.reset_mock()
    transport.edit.reset_mock()
    result = center.publish_notification_thread(
        f"organize:{target}", previous, topic="organize", chat_id="100",
        expected_revision=revision, update_existing_only=True,
    )
    assert not result.accepted
    assert _row()["event_json"] == before["event_json"]
    assert _row()["revision"] == before["revision"]
    assert repository.get_notification(center.notification_thread_event_key(
        "organize:missing", topic="organize", chat_id="100",
    )) is None
    transport.send.assert_not_called()
    transport.edit.assert_not_called()


def test_recovery_reaches_delivered_thread_after_100_policy_blocked_unknown_sends(transport, monkeypatch):
    transport.send.return_value = TelegramSendResult(False, status_code=408, error="timeout")
    unknown_tasks = [f"unknown-{index}" for index in range(100)]
    for index, task in enumerate(unknown_tasks):
        _parent(task=task)
        entries = _refresh_failure(task=task, paths=(f"/isolated-strm/unknown-{index}.strm",))
        assert db.acknowledge_strm_refresh_paths(entries) == 1
        assert _row(task)["status"] == "outcome_unknown"
    transport.send.return_value = TelegramSendResult(True, message_id=501)
    _parent(task="delivered")
    entries = _refresh_failure(task="delivered", paths=("/isolated-strm/delivered.strm",))
    assert db.acknowledge_strm_refresh_paths(entries) == 1
    assert _row("delivered")["status"] == "sent"
    assert transport.send.call_count == 101
    transport.send.reset_mock()
    transport.edit.reset_mock()
    monkeypatch.setattr(policy, "notification_level", lambda: "essential")

    results = [lifecycle.reconcile_organize_refresh_notifications() for _ in range(3)]
    visible = _event("delivered")
    assert "refresh" not in visible.lifecycle, f"前 100 条不能饿死已发送的第 101 条，维护结果 {results}"
    assert visible.state == "completed" and dict(visible.fields)["媒体库"] == QUEUED
    for task in unknown_tasks:
        assert "refresh" not in _event(task).lifecycle
        assert _row(task)["status"] == "outcome_unknown" and not _row(task)["message_id"]
    transport.send.assert_not_called()
    transport.edit.assert_called_once()


@pytest.mark.parametrize("blocker", ["write_refused", "bad_payload"])
def test_bounded_recovery_passes_persistent_blockers_and_revisits_them_after_repair(transport, monkeypatch, blocker):
    blocked_tasks = ["blocked-0", "blocked-1"]
    for task in (*blocked_tasks, "delivered"):
        _parent(task=task)
        entries = _refresh_failure(task=task, paths=(f"/isolated-strm/{task}.strm",))
        assert db.acknowledge_strm_refresh_paths(entries) == 1
    originals = {task: _row(task) for task in blocked_tasks}
    with db.get_conn() as conn:
        if blocker == "write_refused":
            blocked_ids = ",".join(str(int(row["id"])) for row in originals.values())
            # 仅让指定行的事实写入被真实 SQLite 拒绝，后面的合法行仍可提交。
            conn.execute(
                "CREATE TRIGGER refuse_refresh_recovery BEFORE UPDATE OF event_json "
                f"ON telegram_notification_outbox WHEN OLD.id IN ({blocked_ids}) "
                "BEGIN SELECT RAISE(ABORT,'R09 isolated notification write refused'); END"
            )
        else:
            for row in originals.values():
                payload = json.loads(row["event_json"])
                payload["fields"] = {"bad": "shape"}
                # JSON 和 refresh marker 仍合法，因此坏事件确实占用恢复扫描名额。
                conn.execute(
                    "UPDATE telegram_notification_outbox SET event_json=?,revision=revision+1 WHERE id=?",
                    (json.dumps(payload), row["id"]),
                )
    blocked = {task: _row(task) for task in blocked_tasks}
    transport.send.reset_mock()
    transport.edit.reset_mock()
    pages = []
    cycle_start = 0

    def one_row_page(*args, **kwargs):
        kwargs["limit"] = 1
        rows = repository.list_recovered_organize_refresh_notifications(*args, **kwargs)
        pages.append([row["id"] for row in rows])
        assert len(rows) <= 1
        assert len(pages) - cycle_start <= 8, "单轮恢复必须有界推进，不能反复读取同一阻塞页"
        return rows

    monkeypatch.setattr(lifecycle, "list_recovered_organize_refresh_notifications", one_row_page)
    for _ in range(6):
        cycle_start = len(pages)
        lifecycle.reconcile_organize_refresh_notifications()
        if "refresh" not in _event("delivered").lifecycle:
            break
    visible = _event("delivered")
    assert "refresh" not in visible.lifecycle, f"单条拒绝/坏载荷不得挡住后续合法通知，实际页 {pages}"
    assert visible.state == "completed" and dict(visible.fields)["媒体库"] == QUEUED
    for task in blocked_tasks:
        assert _row(task)["event_json"] == blocked[task]["event_json"]
    transport.send.assert_not_called()
    transport.edit.assert_called_once()

    # 推进游标后仍须重访旧行：拒绝解除或坏载荷修复后，原队列应完成恢复。
    with db.get_conn() as conn:
        if blocker == "write_refused":
            conn.execute("DROP TRIGGER refuse_refresh_recovery")
        else:
            for row in originals.values():
                conn.execute(
                    "UPDATE telegram_notification_outbox SET event_json=?,revision=revision+1 WHERE id=?",
                    (row["event_json"], row["id"]),
                )
    for _ in range(6):
        cycle_start = len(pages)
        lifecycle.reconcile_organize_refresh_notifications()
        if all("refresh" not in _event(task).lifecycle for task in blocked_tasks):
            break
    for task in blocked_tasks:
        assert "refresh" not in _event(task).lifecycle
        assert _event(task).state == "completed"
    transport.send.assert_not_called()
    assert transport.edit.call_count == 3
