"""整理→STRM→刷新恢复必须更新原父通知；不重放云端操作。

上游云端整理结果为固定 fixture；不测试云端改名本身。外部 API/TG 为替身。
"""
import json
import socket
import threading
import time
from typing import cast
from unittest.mock import Mock, patch

import tests  # noqa: F401 -- 必须先隔离运行目录
import pytest

from app import database as db
from app.clients.guangya import GuangYaClient, GuangYaFile
from app.modules import media_refresh_coordinator as refresh
from app.modules import organize_tasks, strm
from app.modules import telegram_notification_center as center
from app.modules.organize import Organizer, OrganizeRules
from app.modules.scheduler import STRMScheduler
from app.modules.strm_metadata_worker import STRMMetadataWorker
from app.notifier import TelegramSendResult
from app.repositories.media_refresh_queue import (
    claim_due_media_refreshes, media_refresh_queue_status,
)
from app.repositories.telegram_notifications import get_notification
from tests.support import isolated_test_database
from tests.test_strm_hardening import _TreeClient


def wait_until(predicate):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("isolated worker did not settle within 10s")


@pytest.mark.parametrize("fail_refresh,parent_failed,change_root", [
    (False, False, False), (True, False, False), (True, True, False), (True, False, True),
])
def test_real_handoff_and_refresh_recovery(tmp_path, monkeypatch, fail_refresh, parent_failed, change_root):
    monkeypatch.setattr(socket.socket, "connect", Mock(side_effect=AssertionError("network forbidden")))
    monkeypatch.setattr(socket.socket, "connect_ex", Mock(side_effect=AssertionError("network forbidden")))
    monkeypatch.setattr(socket, "create_connection", Mock(side_effect=AssertionError("network forbidden")))
    with isolated_test_database():
        scheduler = STRMScheduler()
        manager = organize_tasks.OrganizeTaskManager()
        ready = threading.Event()
        finished = threading.Event()
        source = {"id": "source", "name": "isolation", "rel_prefix": strm.STRM_SUBDIR if change_root else "Films"}
        media = GuangYaFile(file_id="v1", name="Movie.mkv", is_dir=False,
                           etag="e1", size=128, parent_id="source")
        client = _TreeClient({"source": [media]})
        change = {"source_id": "source", "kind": "video", "action": "upsert",
                  "file_id": "v1", "parent_id": "source", "name": "Movie.mkv", "rel_dir": ""}

        class CompletedSource(Organizer):
            def __init__(self, client=None):
                assert client is not None
                self.client = client

            def close(self):
                return True

            def _validate_target_outside_source(self, source_id: str, target_id: str) -> None:
                pass

            def organize(self, *_args, **_kwargs):
                return [], {"total": 1 + int(parent_failed), "moved": 1,
                            "failed": int(parent_failed), "need_confirm": 0,
                            "scan_complete": True, "strm_changes": [change]}

        values = {"STRM_ROOT": str(tmp_path), "GY_STRM_BASE_URL": "http://isolated.invalid"}
        for name, value in [("validate_config", ""), ("_source_dirs", [source]),
                            ("_video_exts", {"mkv"}), ("_metadata_exts", set())]:
            monkeypatch.setattr(scheduler, name, Mock(return_value=value))
        for module in ("app.modules.scheduler", "app.modules.organize"):
            monkeypatch.setattr(module + ".get", lambda k, d="": values.get(k, d))
        monkeypatch.setattr("app.modules.scheduler.configured_strm_source_plans", lambda: ([source], ""))
        monkeypatch.setattr("app.modules.scheduler.get_scheduler", lambda: scheduler)
        monkeypatch.setattr(organize_tasks, "Organizer", CompletedSource)
        monkeypatch.setattr(manager, "_wake_download_tracker", lambda: None)
        monkeypatch.setattr("app.modules.download_tracker.resume_relocated_staging", lambda *_a, **_k: [])
        rules = OrganizeRules(target_dir_id="archive", clean_empty=False, link_strm=True,
                              notify_enabled=True, library_notify=True, media_info_enabled=False)
        monkeypatch.setattr(OrganizeRules, "from_config", lambda: rules)
        monkeypatch.setattr("app.modules.telegram_notification_policy.notifications_enabled", lambda: True)
        monkeypatch.setattr("app.modules.telegram_notification_policy.notification_level", lambda: "standard")
        monkeypatch.setattr(center, "notification_target_chat_id", lambda chat="": chat or "100")
        monkeypatch.setattr(center, "_dispatch_stop", threading.Event())
        monkeypatch.setattr(center, "wake_telegram_notification_dispatcher", lambda: None)
        sender = Mock(return_value=TelegramSendResult(True, message_id=123))
        editor = Mock(return_value=TelegramSendResult(True, message_id=123))
        monkeypatch.setattr(center, "send_event_result", sender)
        monkeypatch.setattr(center, "edit_event_result", editor)
        # 只调度明确的一轮，以免后台常驻消费者改变实验时序。
        monkeypatch.setattr("app.modules.strm_metadata_worker._worker.wake", lambda: None)
        monkeypatch.setattr(refresh, "_configured_provider_names", lambda **_k: ("jellyfin",))
        monkeypatch.setattr(refresh, "get_media_refresh_coordinator", lambda: Mock())

        def sync(**kwargs):
            assert ready.wait(10), "parent result was not persisted"
            result = strm.sync_strm_incremental(client=cast(GuangYaClient, client), **kwargs)
            if change_root:
                # 已落盘路径可由既有 worker 提前交接，随后配置才切换。
                STRMMetadataWorker()._flush_media_refresh(force=True)
                values["STRM_ROOT"] = str(tmp_path / strm.STRM_SUBDIR)
            return result

        monkeypatch.setattr("app.modules.scheduler.sync_strm_incremental", sync)
        monkeypatch.setattr("app.modules.scheduler.sync_strm", lambda **kw: strm.sync_strm(client=cast(GuangYaClient, client), **kw))
        real_execute = scheduler._execute_locked

        def execute(*args):
            try:
                return real_execute(*args)
            finally:
                finished.set()

        monkeypatch.setattr(scheduler, "_execute_locked", execute)
        real_enqueue = refresh.enqueue_media_refresh
        enqueue_calls = []

        def enqueue(*args, **kwargs):
            enqueue_calls.append(args)
            if fail_refresh and len(enqueue_calls) == (2 if change_root else 1):
                raise OSError("isolated transient refresh-queue write failure")
            return real_enqueue(*args, **kwargs)

        monkeypatch.setattr(refresh, "enqueue_media_refresh", enqueue)
        admission = manager.start([source], rules, chat_id="100", client=client,
                                  delivery_route={"chat_id": "100", "message_thread_id": 500})
        assert admission["ok"], admission
        try:
            wait_until(lambda: manager.status()["status"] != "running")
            parent_worker = manager._worker
            if parent_worker:
                parent_worker.join(10)
                assert not parent_worker.is_alive()
            parent = manager.status()
            assert parent["status"] == ("partial" if parent_failed else "completed"), parent
            persisted = db.get_task_run(parent["run_id"])
            assert persisted is not None
            assert persisted["status"] == ("partial" if parent_failed else "success")
            assert parent["stats"]["failed"] == int(parent_failed)
            assert parent["stats"]["need_confirm"] == 0
            if not parent_failed:
                assert not any(parent["stats"].get(key) for key in (
                    "scan_errors", "scan_limited", "stopped", "audit_failures",
                    "empty_dir_cleanup_failed", "source_dir_cleanup_failed",
                    "replacement_cleanup_failed", "protected_staging_dirs",
                ))
            key = center.notification_thread_event_key(
                f"organize:{parent['id']}", topic="organize", chat_id="100")

            def snapshot():
                row = get_notification(key)
                assert row is not None
                event = json.loads(row["event_json"])
                return {"revision": row["revision"], "delivered_revision": row["delivered_revision"],
                        "event_key": row["event_key"], "state": event.get("state"),
                        "title": event["title"], "fields": event.get("fields"),
                        "footer": event.get("footer"), "route": event.get("_delivery_route")}

            before = snapshot()
            assert before["state"] == "queued" and before["revision"] == 1
            ready.set()
            assert finished.wait(10)
            after_failure = snapshot()
            entries_before = db.list_strm_refresh_entries()
            assert len(entries_before) == int(fail_refresh)
            assert len(list(tmp_path.rglob("*.strm"))) == 1
            if fail_refresh:
                from app.modules.telegram_organize_lifecycle import reconcile_organize_refresh_notifications

                # 路径依据必须是实际入队项；运行中配置变化不能让未 ACK 的工作消失。
                assert reconcile_organize_refresh_notifications() == 0
            # 新 worker 实例从 SQLite 读取失败遗留项；不传入通知 ID 或伪造成功回调。
            STRMMetadataWorker()._flush_media_refresh(force=True)
            assert db.count_strm_refresh_paths() == 0
            coordinator = refresh.MediaRefreshCoordinator()
            api = Mock(display_name="Jellyfin")
            api.refresh_for_paths.return_value = {"ok": True, "scope": "item", "requested": 1}
            with patch.object(coordinator, "_client_for", return_value=api):
                claimed = claim_due_media_refreshes(owner=coordinator._owner, now_epoch=time.time() + 400)
                assert len(claimed) == 1
                coordinator._process_group(claimed[0])
            assert coordinator.status()["completed_session"] == 1
            assert all(media_refresh_queue_status()[key] == 0
                       for key in ("queued", "running", "retry_wait", "paths"))
            assert api.refresh_for_paths.call_count == 1
            after_recovery = snapshot()
            assert after_recovery["route"] == {"chat_id": "100", "message_thread_id": 500}
            if fail_refresh:
                assert after_failure["state"] == "partial"
                assert "等待后台重试" in after_failure["footer"]
                assert after_recovery["revision"] == after_failure["revision"] + 1
                assert after_recovery["delivered_revision"] == after_recovery["revision"]
                assert "等待后台重试" not in after_recovery["footer"]
                assert dict(after_recovery["fields"])["媒体库"] == "已排队"
            else:
                assert after_recovery == after_failure
            assert after_recovery["state"] == ("partial" if parent_failed else "completed")
            assert after_recovery["title"] == ("⚠️ 光鸭整理部分完成" if parent_failed else "✅ 光鸭整理完成")
            assert sender.call_count == 1
            STRMMetadataWorker()._flush_media_refresh(force=True)
            assert snapshot() == after_recovery  # 重复消费不能重复编辑或重跑 STRM。
            assert len(enqueue_calls) == 1 + int(fail_refresh) + int(change_root)
        finally:
            ready.set()
            for worker in (scheduler._worker, manager._worker):
                if worker:
                    worker.join(10)
