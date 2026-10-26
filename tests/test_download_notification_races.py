"""下载通知快照竞争：真实 SQLite 与线程，Telegram 仅使用替身。"""
from __future__ import annotations

import os
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app import database as db
from app.modules.agent_download_verification_notifications import dump_download_verification_payload
from app.modules import telegram_download_lifecycle as lifecycle
from app.modules import telegram_notification_center as center
from app.modules.telegram_notification_policy import NotificationTopic
from app.notifier import TelegramSendResult
from tests.support import isolated_test_database


@pytest.fixture
def transport():
    with isolated_test_database("download-notification-races.db"), patch.dict(os.environ, {
        "TG_CHAT_ID": "100", "TG_NOTIFICATION_ENABLED": "1",
        "TG_NOTIFICATION_LEVEL": "standard", "GY_ORGANIZE_NOTIFY_ENABLED": "1",
    }), patch.object(center, "_dispatch_stop", threading.Event()), patch.object(
        center, "wake_telegram_notification_dispatcher",
    ), patch.object(
        center, "send_event_result", return_value=TelegramSendResult(True, message_id=72),
    ) as send, patch.object(
        center, "edit_event_result", return_value=TelegramSendResult(True, message_id=72),
    ) as edit, patch(
        "socket.socket.connect", side_effect=AssertionError("R08 forbids network"),
    ), patch(
        "socket.socket.connect_ex", side_effect=AssertionError("R08 forbids network"),
    ), patch(
        "socket.create_connection", side_effect=AssertionError("R08 forbids network"),
    ):
        yield SimpleNamespace(send=send, edit=edit)


def _request():
    request_id, _ = db.create_download_request(
        "notification-race", "magnet", title="Race Film", origin="agent", chat_id="100",
    )
    db.update_download_request(request_id, targets="qb", status="downloading", qb_status="downloading")
    return request_id


def _snapshot(request_id):
    snapshot = center.get_notification_thread_snapshot(
        f"download:{request_id}", topic=NotificationTopic.DOWNLOAD, chat_id="100",
    )
    assert snapshot is not None
    return snapshot


def _race(request_id, *, boundary, advance, publish_kwargs=None):
    ready, resume = threading.Event(), threading.Event()
    errors, results = [], []
    original = lifecycle.publish_notification_thread if boundary == "publish" else db.get_download_request
    gated = False

    def gate(*args, **kwargs):
        nonlocal gated
        value = original(*args, **kwargs) if boundary == "read" else None
        if threading.current_thread().name == "late-notification" and not gated:
            gated = True
            ready.set()
            if not resume.wait(10):
                raise AssertionError("new publisher did not release the old snapshot")
        return value if boundary == "read" else original(*args, **kwargs)

    def publish_old():
        try:
            results.append(lifecycle.publish_download_lifecycle(request_id, **(publish_kwargs or {})))
        except BaseException as exc:
            errors.append(exc)

    target, name = (lifecycle, "publish_notification_thread") if boundary == "publish" else (db, "get_download_request")
    with patch.object(target, name, gate):
        worker = threading.Thread(target=publish_old, name="late-notification")
        worker.start()
        try:
            assert ready.wait(10), "old publisher did not reach the snapshot boundary"
            advance()
            fresh = _snapshot(request_id)
        finally:
            resume.set()
            worker.join(10)
        assert not worker.is_alive()
    assert not errors
    assert len(results) == 1 and results[0].accepted
    return fresh


@pytest.mark.parametrize("existing_thread", [False, True])
@pytest.mark.parametrize("boundary", ["publish", "read"])
def test_late_download_snapshot_cannot_overwrite_completed_thread(transport, existing_thread, boundary):
    request_id = _request()
    if existing_thread:
        assert lifecycle.publish_download_lifecycle(request_id).delivered

    def complete():
        db.update_download_request(request_id, status="completed", qb_status="completed", local_import_status="skipped")
        assert lifecycle.publish_download_lifecycle(request_id).delivered

    fresh = _race(request_id, boundary=boundary, advance=complete)
    final = _snapshot(request_id)
    assert fresh.event.state == "downloaded"
    assert final.event == fresh.event
    assert final.revision == fresh.revision
    assert final.current_revision_delivered
    row = db.get_download_request(request_id)
    assert row is not None
    assert row["status"] == row["qb_status"] == "completed"
    assert transport.send.call_count == 1
    if existing_thread:
        assert transport.edit.call_count == 1
    else:
        transport.edit.assert_not_called()


def test_retry_can_return_to_processing_without_late_completed_snapshot_restoring_success(transport):
    request_id = _request()
    db.update_download_request(request_id, status="completed", qb_status="completed", local_import_status="skipped")
    assert lifecycle.publish_download_lifecycle(request_id).delivered

    def retry():
        db.update_download_request(request_id, status="downloading", qb_status="downloading", local_import_status="")
        assert lifecycle.publish_download_lifecycle(request_id).delivered

    fresh = _race(request_id, boundary="publish", advance=retry)
    final = _snapshot(request_id)
    assert fresh.event.state == "processing"
    assert final.event == fresh.event
    assert final.revision == fresh.revision
    assert final.current_revision_delivered
    assert transport.send.call_count == transport.edit.call_count == 1


@pytest.mark.parametrize("publish_new", [False, True])
def test_current_business_state_is_checked_even_when_latest_publication_keeps_revision(transport, publish_new):
    request_id = _request()
    db.update_download_request(request_id, status="completed", qb_status="completed", local_import_status="skipped")
    assert lifecycle.publish_download_lifecycle(request_id).delivered
    original = _snapshot(request_id)
    # 重试尚未发布进度，随后再次完成；最新通知与原终态相同，不会递增 revision。
    db.update_download_request(request_id, status="downloading", qb_status="downloading", local_import_status="")

    def complete():
        db.update_download_request(request_id, status="completed", qb_status="completed", local_import_status="skipped")
        if publish_new:
            assert lifecycle.publish_download_lifecycle(request_id).delivered

    fresh = _race(request_id, boundary="publish", advance=complete)
    final = _snapshot(request_id)
    assert fresh.revision == original.revision
    assert final.event == fresh.event
    assert final.revision == fresh.revision
    assert final.current_revision_delivered
    transport.edit.assert_not_called()


@pytest.mark.parametrize("terminal_status,terminal_result", [("visible", "visible"), ("attention", "missing")])
@pytest.mark.parametrize("publish_new", [False, True])
def test_verification_retry_is_reread_after_notification_conflict(transport, terminal_status, terminal_result, publish_new):
    request_id = _request()
    db.update_download_request(
        request_id, status="completed", qb_status="completed", local_import_status="completed", strm_status="completed",
    )
    assert db.enqueue_agent_download_verification(
        request_id, title="Race Film", tmdb_id="123", season=1, episode=1,
        as_of="2026-10-26", owner="tg:v1:100\x1f200", chat_id="100",
    )
    job = db.claim_due_agent_download_verification()
    assert job is not None
    assert db.finish_agent_download_verification(
        request_id, status=terminal_status, result=terminal_result, attempts=1, next_check_at=db.now(),
        expected_lease_generation=job["lease_generation"],
        payload_json=dump_download_verification_payload(
            title="Race Film", season=1, episode=1, status=terminal_status, result=terminal_result, attempts=1,
        ),
    )
    assert lifecycle.publish_download_lifecycle(request_id).delivered
    assert _snapshot(request_id).event.state == ("completed" if terminal_status == "visible" else "attention")

    def retry():
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE agent_download_verifications SET status='retry_wait',result='',lease_generation=lease_generation+1 "
                "WHERE request_id=?", (request_id,),
            )
        if publish_new:
            assert lifecycle.publish_download_lifecycle(request_id).delivered

    fresh = _race(request_id, boundary="publish", advance=retry)
    final = _snapshot(request_id)
    assert final.event.state == "processing"
    assert dict(final.event.fields)["入库复核"] == "等待复核重试"
    if publish_new:
        assert final.event == fresh.event
        assert final.revision == fresh.revision
    else:
        assert final.revision == fresh.revision + 1
    assert final.current_revision_delivered


def test_conflicting_callback_does_not_reapply_old_refresh_hint(transport):
    request_id = _request()
    assert lifecycle.publish_download_lifecycle(request_id).delivered

    def complete():
        db.update_download_request(request_id, status="completed", qb_status="completed", local_import_status="skipped")
        assert lifecycle.publish_download_lifecycle(request_id, media_refresh="刷新完成").delivered

    fresh = _race(
        request_id, boundary="publish", advance=complete,
        publish_kwargs={"media_refresh": "等待刷新", "stats": {"media_items": [{"title": "Old Film"}]}},
    )
    final = _snapshot(request_id)
    assert dict(final.event.fields)["媒体库"] == "刷新完成"
    assert final.event == fresh.event
    assert final.revision == fresh.revision
