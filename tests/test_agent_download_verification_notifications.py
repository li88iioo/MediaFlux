"""复核通知必须使用真实请求、当前复核事实和统一下载消息 outbox。"""
from __future__ import annotations

import os
import threading
import unittest
from datetime import datetime, timedelta
from inspect import Parameter, signature
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from starlette.requests import Request

from app import database as db
from app import notifier
from app.modules import telegram_notification_center as center
from app.modules.agent_download_verification_notifications import (
    dump_download_verification_payload,
    load_download_verification_payload,
    notify_download_verification_terminal_result,
)
from app.modules.agent_download_verification_scheduler import (
    DownloadLibraryVerificationScheduler,
)
from app.modules.telegram_download_lifecycle import publish_download_lifecycle
from app.modules.telegram_notification_center import (
    get_notification_thread_snapshot,
    notification_thread_event_key,
)
from app.modules.telegram_notification_policy import NotificationTopic
from app.notifier import TelegramSendResult, render_event
from app.repositories import (
    agent_download_verification as agent_download_verification_repo,
)
from app.repositories import download_requests as download_requests_repo
from app.repositories.telegram_notifications import get_notification
from app.routes.api import save_config
from tests.support import (
    IsolatedDatabaseTestCase,
    authenticated_session,
    preserve_web_auth_config,
)


class DownloadVerificationNotificationTests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM agent_download_verification_notification_outbox")
            conn.execute("DELETE FROM agent_download_verifications")
            conn.execute("DELETE FROM telegram_notification_outbox")
            conn.execute("DELETE FROM download_requests")
        self.enterContext(patch.dict(os.environ, {
            "AGENT_LLM_ENABLED": "1", "TG_AGENT_ENABLED": "1", "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200", "TG_NOTIFICATION_ENABLED": "1",
            "TG_NOTIFICATION_LEVEL": "standard", "AGENT_DOWNLOAD_VERIFICATION_NOTIFY_ENABLED": "1",
            "GY_ORGANIZE_NOTIFY_ENABLED": "1",
        }))
        # 默认通知路由来自 transport 的已初始化配置；不继承其他用例留下的单例。
        self.enterContext(patch.multiple(notifier, _initialized=True, _chat_id="100", _bot=None))
        for target in ("socket.socket.connect", "socket.socket.connect_ex", "socket.create_connection"):
            self.enterContext(patch(target, side_effect=AssertionError("R07 forbids network")))
        self.enterContext(patch.object(center, "_dispatch_stop", threading.Event()))
        self.enterContext(patch.object(center, "wake_telegram_notification_dispatcher"))
        self.sender = self.enterContext(patch.object(
            center, "send_event_result", return_value=TelegramSendResult(True, message_id=73),
        ))
        self.editor = self.enterContext(patch.object(
            center, "edit_event_result", return_value=TelegramSendResult(True, message_id=73),
        ))

    def _request(self, *, status="visible", result="visible", payload_title="The <Show>",
                 request_chat_id="100", origin="agent"):
        request_id = self._completed_request(request_chat_id=request_chat_id, origin=origin)
        self._finish_verification(request_id, status=status, result=result, payload_title=payload_title)
        return request_id

    def _completed_request(self, *, request_chat_id="100", origin="agent"):
        request_id, _ = download_requests_repo.create_download_request(
            f"{self.id()}:{origin}", "magnet", title="The <Show> S02E03", origin=origin, chat_id=request_chat_id,
        )
        download_requests_repo.update_download_request(
            request_id, targets="qb", status="completed", qb_status="completed",
            local_import_status="completed", strm_status="completed",
        )
        return request_id

    def _finish_verification(self, request_id, *, status="visible", result="visible", payload_title="The <Show>"):
        self.assertEqual(agent_download_verification_repo.enqueue_agent_download_verification(
            request_id,
            targets=[{"title": "The <Show>", "tmdb_id": "123", "season": 2, "episode": 3, "as_of": "2026-10-26"}],
            owner="tg:v1:100\x1f200", chat_id="100",
        ), "queued")
        job = agent_download_verification_repo.claim_due_agent_download_verification()
        assert job is not None
        serialized = dump_download_verification_payload(
            targets=[{"title": payload_title, "season": 2, "episode": 3, "result": result}],
            status=status, result=result, attempts=2,
        )
        payload = load_download_verification_payload(serialized)
        self.assertTrue(agent_download_verification_repo.finish_agent_download_verification(
            request_id, status=payload["status"], result=payload["result"], attempts=payload["attempts"],
            next_check_at=db.now(), expected_lease_generation=job["lease_generation"], payload_json=serialized,
            targets=[{**target, "result": payload["result"]} for target in job["targets"]],
        ))

    def _backdate_download(self, request_id: int) -> None:
        completed_at = (datetime.now() - timedelta(days=8)).isoformat(sep=" ", timespec="seconds")
        # 保留近期 updated_at，避免把通用记录更新当成新的业务完成时间。
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE download_requests SET created_at=?,completed_at=?,"
                "local_import_completed_at=?,strm_finished_at=? WHERE id=?",
                (completed_at, completed_at, completed_at, completed_at, request_id),
            )

    def _historical_request_with_message(self) -> tuple[int, str]:
        request_id = self._completed_request()
        original = publish_download_lifecycle(request_id)
        self.assertTrue(original.delivered)
        self.sender.reset_mock()
        self.editor.reset_mock()
        self._backdate_download(request_id)
        self._finish_verification(request_id)
        return request_id, original.event_key

    def _verification_outbox(self, request_id: int) -> dict:
        rows = [
            dict(row) for row in agent_download_verification_repo.list_agent_download_verification_notifications()
            if row["request_id"] == request_id
        ]
        self.assertEqual(len(rows), 1)
        return rows[0]

    def _snapshot(self, request_id):
        snapshot = get_notification_thread_snapshot(
            f"download:{request_id}", topic=NotificationTopic.DOWNLOAD, chat_id="100",
        )
        assert snapshot is not None
        return snapshot

    def _event(self, request_id):
        return self._snapshot(request_id).event

    def test_visible_event_contains_only_safe_fixed_fields(self):
        request_id = self._request()
        self.assertTrue(notify_download_verification_terminal_result(
            owner="tg:v1:100\x1f200", chat_id="100", request_id=request_id,
        ).ok)
        event = self._event(request_id)

        rendered = render_event(event)
        self.assertEqual(event.layout, "relaxed")
        self.assertEqual(event.state, "completed")
        self.assertIn("下载与入库完成", rendered)
        self.assertIn("The &lt;Show&gt;", rendered)
        self.assertIn("S02E03", rendered)
        self.assertIn("入库复核", rendered)
        self.assertIn("已在媒体库中可见", rendered)
        for forbidden in ("request_id", "magnet:", "/volume/", "token", "tmdb_id"):
            self.assertNotIn(forbidden, rendered.lower())

    def test_attention_event_uses_fixed_reason_and_safe_fallback(self):
        request_id = self._request(status="attention", result="private backend error", payload_title="\x00unsafe")
        self.assertTrue(notify_download_verification_terminal_result(
            owner="tg:v1:100\x1f200", chat_id="100", request_id=request_id,
        ).ok)

        event = self._event(request_id)
        rendered = render_event(event)
        self.assertEqual(event.state, "attention")
        self.assertIn("下载入库需要处理", rendered)
        self.assertIn("无法得出可靠结论", rendered)
        self.assertNotIn("private backend error", rendered)
        payload = load_download_verification_payload(agent_download_verification_repo.list_agent_download_verification_notifications()[0]["payload_json"])
        self.assertEqual(payload["targets"][0]["title"], "未命名剧集")
        self.assertEqual(payload["result"], "inconclusive")

    def test_sensitive_title_pattern_is_not_rendered_or_logged(self):
        request_id = self._request(
            status="attention", result="missing", payload_title="https://private.invalid/file?token=SECRET",
        )
        self.assertTrue(notify_download_verification_terminal_result(
            owner="tg:v1:100\x1f200", chat_id="100", request_id=request_id,
        ).ok)

        rendered = render_event(self._event(request_id))
        payload = agent_download_verification_repo.list_agent_download_verification_notifications()[0]["payload_json"]
        self.assertEqual(load_download_verification_payload(payload)["targets"][0]["title"], "未命名剧集")
        self.assertNotIn("private.invalid", payload)
        self.assertNotIn("SECRET", payload)
        self.assertNotIn("private.invalid", rendered)
        self.assertNotIn("SECRET", rendered)

    def test_persistent_payload_is_safe_and_strict(self):
        serialized = dump_download_verification_payload(targets=[{'title': 'https://private.invalid/file?token=SECRET', 'season': 2, 'episode': 3, 'result': 'visible'}], status='visible', result='visible', attempts=2)
        self.assertNotIn("private.invalid", serialized)
        self.assertNotIn("SECRET", serialized)
        self.assertEqual(load_download_verification_payload(serialized), {
            "targets": [{"title": "未命名剧集", "season": 2, "episode": 3, "result": "visible"}],
            "status": "visible",
            "result": "visible",
            "attempts": 2,
        })
        with self.assertRaises(ValueError):
            load_download_verification_payload('{"title":"x","secret":"no"}')

    def test_visible_library_with_partial_backend_still_notifies_overall_attention(self):
        request_id = self._request()
        download_requests_repo.update_download_request(request_id, targets="both", status="manual_review",
                                   qb_status="completed", gy_status="manual_review")
        self.assertTrue(notify_download_verification_terminal_result(
            owner="tg:v1:100\x1f200", chat_id="100", request_id=request_id,
        ).ok)
        event = self._event(request_id)
        self.assertEqual(event.state, "attention")
        self.assertIn("已在媒体库中可见", render_event(event))
        assert isinstance(event.title, str)
        self.assertNotIn("下载与入库完成", event.title)

    def test_disabled_notification_never_calls_sender(self):
        request_id = self._request()
        with patch.dict(os.environ, {"AGENT_DOWNLOAD_VERIFICATION_NOTIFY_ENABLED": "0"}):
            sent = notify_download_verification_terminal_result(
                owner="tg:v1:100\x1f200", chat_id="100", request_id=request_id,
            )

        self.assertFalse(sent.ok)
        self.sender.assert_not_called()
        self.assertEqual(agent_download_verification_repo.list_agent_download_verification_notifications()[0]["status"], "pending")

    def test_enabled_notification_calls_sender(self):
        request_id = self._request()
        for _ in range(2):
            sent = notify_download_verification_terminal_result(
                owner="tg:v1:100\x1f200", chat_id="100", request_id=request_id,
            )
            self.assertTrue(sent.ok)
        self.sender.assert_called_once()
        event_key = notification_thread_event_key(
            f"download:{request_id}", topic=NotificationTopic.DOWNLOAD, chat_id="100",
        )
        row = get_notification(event_key)
        assert row is not None
        self.assertEqual(row["thread_key"], f"download:{request_id}")
        self.assertEqual(row["chat_id"], "100")
        self.assertEqual(row["status"], "sent")
        self.assertEqual(
            self._event(request_id).fields[0], ("媒体", "The <Show> S02E03"),
        )
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM telegram_notification_outbox").fetchone()[0], 1)

    def test_historical_verification_without_original_message_only_settles(self):
        for previously_queued in (False, True):
            with self.subTest(previously_queued=previously_queued):
                request_id = self._request()
                event_key = notification_thread_event_key(
                    f"download:{request_id}", topic=NotificationTopic.DOWNLOAD, chat_id="100",
                )
                self.sender.reset_mock()
                if previously_queued:
                    # 模拟升级前已排队、从未拿到 Telegram 回执的复核终态卡。
                    self.assertTrue(publish_download_lifecycle(request_id, deliver_now=False).queued)
                    queued = get_notification(event_key)
                    assert queued is not None
                    self.assertEqual((queued["status"], queued["message_id"]), ("pending", None))
                self._backdate_download(request_id)

                center.drain_telegram_notifications(event_key=event_key)
                self.sender.assert_not_called()
                telegram_row = get_notification(event_key)
                if previously_queued:
                    assert telegram_row is not None
                    self.assertEqual(
                        (telegram_row["status"], telegram_row["last_error"], telegram_row["message_id"]),
                        ("suppressed", "HistoricalVerification", None),
                    )
                else:
                    self.assertIsNone(telegram_row)
                self.assertEqual(DownloadLibraryVerificationScheduler().dispatch_notification_once(), 1)
                settled = self._verification_outbox(request_id)
                self.assertEqual(DownloadLibraryVerificationScheduler().dispatch_notification_once(), 0)
                center.drain_telegram_notifications(event_key=event_key)

                self.sender.assert_not_called()
                self.editor.assert_not_called()
                self.assertEqual(
                    (settled["status"], settled["last_error_type"], settled["attempts"], settled["payload_json"]),
                    ("discarded", "HistoricalVerification", 0, ""),
                )
                self.assertEqual(get_notification(event_key), telegram_row)
                self.assertEqual(self._verification_outbox(request_id), settled)
                job = agent_download_verification_repo.get_agent_download_verification(request_id)
                assert job is not None
                self.assertEqual((job["status"], job["result"]), ("visible", "visible"))

    def test_historical_verification_edits_original_message_even_after_rate_limit(self):
        for rate_limited in (False, True):
            with self.subTest(rate_limited=rate_limited):
                request_id, event_key = self._historical_request_with_message()
                if rate_limited:
                    self.editor.side_effect = [
                        TelegramSendResult(False, error="Too Many Requests", status_code=429, retry_after_seconds=120),
                        TelegramSendResult(True, message_id=73),
                    ]
                scheduler = DownloadLibraryVerificationScheduler()
                self.assertEqual(scheduler.dispatch_notification_once(), 1)
                if rate_limited:
                    pending = get_notification(event_key)
                    assert pending is not None
                    self.assertEqual(
                        (pending["status"], pending["attempts"], pending["message_id"]), ("retry_wait", 1, 73),
                    )
                    self.assertGreater(pending["next_attempt_at"], db.now())
                    center.drain_telegram_notifications(event_key=event_key)
                    self.editor.assert_called_once()
                    with db.get_conn() as conn:
                        conn.execute(
                            "UPDATE telegram_notification_outbox SET next_attempt_at=? WHERE event_key=?",
                            (db.now(), event_key),
                        )
                    self.assertTrue(center.drain_telegram_notifications(event_key=event_key))
                    self.assertEqual(self.editor.call_args_list[0], self.editor.call_args_list[1])

                self.assertEqual(scheduler.dispatch_notification_once(), 0)
                center.drain_telegram_notifications(event_key=event_key)
                self.sender.assert_not_called()
                self.assertEqual(self.editor.call_count, 2 if rate_limited else 1)
                self.assertEqual(self.editor.call_args.kwargs, {"chat_id": "100", "message_id": 73})
                snapshot = self._snapshot(request_id)
                self.assertTrue(snapshot.current_revision_delivered)
                self.assertEqual(snapshot.message_id, 73)
                self.assertIn("已在媒体库中可见", render_event(snapshot.event))
                self.assertEqual(self._verification_outbox(request_id)["status"], "sent")

    def test_historical_verification_does_not_replace_deleted_or_uneditable_message(self):
        for description, deferred in (("message to edit not found", False), ("message can't be edited", True)):
            with self.subTest(description=description, deferred=deferred):
                request_id, event_key = self._historical_request_with_message()
                self.editor.return_value = TelegramSendResult(
                    False, error=f"Bad Request: {description}", status_code=400,
                )
                scheduler = DownloadLibraryVerificationScheduler()
                stopped = threading.Event()
                if deferred:
                    # 回到独立投递轮次，从 SQLite 读取已交接的编辑任务。
                    stopped.set()
                with patch.object(center, "_dispatch_stop", stopped):
                    self.assertEqual(scheduler.dispatch_notification_once(), 1)
                if deferred:
                    self.editor.assert_not_called()
                    queued = get_notification(event_key)
                    assert queued is not None
                    self.assertEqual((queued["status"], queued["message_id"]), ("pending", 73))
                    center.drain_telegram_notifications(event_key=event_key)

                settled = self._verification_outbox(request_id)
                self.assertEqual(DownloadLibraryVerificationScheduler().dispatch_notification_once(), 0)
                center.recover_notifications()
                center.drain_telegram_notifications(event_key=event_key)

                self.sender.assert_not_called()
                self.editor.assert_called_once()
                self.assertEqual(self.editor.call_args.kwargs, {"chat_id": "100", "message_id": 73})
                snapshot = self._snapshot(request_id)
                self.assertEqual(
                    (snapshot.status, snapshot.last_error, snapshot.message_id), ("suppressed", "HistoricalVerification", 0),
                )
                self.assertFalse(snapshot.current_revision_delivered)
                expected = ("sent", "") if deferred else ("discarded", "HistoricalVerification")
                self.assertEqual((settled["status"], settled["last_error_type"]), expected)
                self.assertEqual(settled["payload_json"], "")
                self.assertEqual(self._verification_outbox(request_id), settled)

    def test_old_request_with_recent_organize_completion_still_sends(self):
        request_id = self._completed_request()
        self._backdate_download(request_id)
        organized_at = (datetime.now() - timedelta(days=6)).isoformat(sep=" ", timespec="seconds")
        download_requests_repo.update_download_request(
            request_id, targets="guangya", qb_status="", gy_status="completed",
            local_import_status="", local_import_completed_at=None, strm_status="", strm_finished_at=None,
            organize_status="completed", organize_finished_at=organized_at,
        )
        self._finish_verification(request_id)

        self.assertEqual(DownloadLibraryVerificationScheduler().dispatch_notification_once(), 1)
        self.assertEqual(DownloadLibraryVerificationScheduler().dispatch_notification_once(), 0)
        center.drain_telegram_notifications()

        self.sender.assert_called_once()
        self.editor.assert_not_called()
        self.assertEqual(self.sender.call_args.kwargs["chat_id"], "100")
        self.assertIn("已在媒体库中可见", render_event(self._event(request_id)))
        self.assertEqual(self._verification_outbox(request_id)["status"], "sent")

    def test_cross_chat_verification_does_not_publish_or_join_original_download_thread(self):
        for origin, request_chat in (("indexer:nyaa", "999"), ("rss", "100")):
            with self.subTest(origin=origin):
                self.sender.reset_mock()
                request_id = self._request(origin=origin, request_chat_id=request_chat)
                if origin == "rss":
                    download_requests_repo.update_download_request(request_id, notification_payload_json='{"chat_id":"999"}')
                stored_row_2 = download_requests_repo.get_download_request(request_id)
                assert stored_row_2 is not None
                request = dict(stored_row_2)
                job = agent_download_verification_repo.get_agent_download_verification(request_id)

                sent = notify_download_verification_terminal_result(
                    owner="tg:v1:100\x1f200", chat_id="100", request_id=request_id,
                )
                self.assertFalse(sent.ok)
                self.assertEqual(sent.error, "InvalidRoute")
                self.sender.assert_not_called()
                self.assertEqual(DownloadLibraryVerificationScheduler().dispatch_notification_once(), 1)
                notification = next(item for item in agent_download_verification_repo.list_agent_download_verification_notifications()
                                    if item["request_id"] == request_id)
                self.assertEqual(notification["status"], "discarded")
                self.assertEqual(notification["last_error_type"], "InvalidRoute")
                self.assertEqual(agent_download_verification_repo.get_agent_download_verification(request_id), job)
                stored_row_3 = download_requests_repo.get_download_request(request_id)
                assert stored_row_3 is not None
                self.assertEqual(dict(stored_row_3), request)

                self.assertTrue(publish_download_lifecycle(request_id).delivered)
                self.sender.assert_called_once()
                self.assertEqual(self.sender.call_args.kwargs["chat_id"], "999")
                self.assertNotIn("入库复核", dict(self.sender.call_args.args[0].fields))
                self.assertIsNone(get_notification_thread_snapshot(
                    f"download:{request_id}", topic=NotificationTopic.DOWNLOAD, chat_id="100",
                ))
                stored_row_4 = download_requests_repo.get_download_request(request_id)
                assert stored_row_4 is not None
                self.assertEqual(dict(stored_row_4), request)

    def test_request_without_chat_keeps_existing_default_route(self):
        request_id = self._request(request_chat_id="", origin="rss")
        self.assertTrue(notify_download_verification_terminal_result(
            owner="tg:v1:100\x1f200", chat_id="100", request_id=request_id,
        ).ok)
        self.assertEqual(self.sender.call_args.kwargs["chat_id"], "100")
        self.assertIn("入库复核", dict(self.sender.call_args.args[0].fields))
        stored_row_1 = download_requests_repo.get_download_request(request_id)
        assert stored_row_1 is not None
        self.assertEqual(stored_row_1["chat_id"], "")

    def test_revoked_route_never_calls_sender(self):
        request_id = self._request()
        for key, value in (
            ("AGENT_LLM_ENABLED", "0"),
            ("TG_AGENT_ENABLED", "0"),
            ("TG_CHAT_ID", "999"),
            ("TG_AGENT_ALLOWED_USER_IDS", "201"),
        ):
            with self.subTest(key=key), patch.dict(
                os.environ,
                {key: value},
                clear=False,
            ):
                sent = notify_download_verification_terminal_result(
                    owner="tg:v1:100\x1f200", chat_id="100", request_id=request_id,
                )

            self.assertFalse(sent.ok)
            self.sender.assert_not_called()

    def test_invalid_or_missing_route_never_uses_global_sender(self):
        request_id = self._request()
        for owner, chat_id in (("web:v1:abc", ""), ("", "100"), ("tg:v1:100\x1f200", "999")):
            sent = notify_download_verification_terminal_result(
                owner=owner, chat_id=chat_id, request_id=request_id,
            )
            self.assertFalse(sent.ok)
        self.sender.assert_not_called()

    def test_request_id_is_required_and_missing_request_never_creates_notification(self):
        parameters = signature(notify_download_verification_terminal_result).parameters
        self.assertIs(parameters["request_id"].default, Parameter.empty)
        self.assertEqual(set(parameters), {"owner", "chat_id", "request_id"})
        for request_id in (0, -1, 999999):
            self.assertFalse(notify_download_verification_terminal_result(
                owner="tg:v1:100\x1f200", chat_id="100", request_id=request_id,
            ).ok)
        self.sender.assert_not_called()
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM telegram_notification_outbox").fetchone()[0], 0)

    def test_unknown_send_stays_in_single_outbox_without_duplicate_retry(self):
        request_id = self._request()
        self.sender.return_value = TelegramSendResult(False, error="ReadTimeout", status_code=0)
        scheduler = DownloadLibraryVerificationScheduler()
        self.assertEqual(scheduler.dispatch_notification_once(), 1)
        self.assertEqual(scheduler.dispatch_notification_once(), 0)
        center.drain_telegram_notifications()
        self.sender.assert_called_once()
        row = get_notification(notification_thread_event_key(
            f"download:{request_id}", topic=NotificationTopic.DOWNLOAD, chat_id="100",
        ))
        assert row is not None
        self.assertEqual(row["status"], "outcome_unknown")
        self.assertEqual(agent_download_verification_repo.list_agent_download_verification_notifications()[0]["status"], "sent")

    def test_notification_switch_is_accepted_by_config_api(self):
        request = SimpleNamespace(
            session=authenticated_session(),
            app=SimpleNamespace(
                state=SimpleNamespace(
                    background_services_enabled=False,
                    media_proxy_manager=None,
                )
            ),
        )
        with patch("app.routes.api.config.get", side_effect=preserve_web_auth_config(lambda _key, default="": "")), patch(
            "app.routes.api.config.set_and_save"
        ) as persist, patch("app.services.clear_dashboard_cache"):
            response = save_config(
                cast(Request, request),
                {"AGENT_DOWNLOAD_VERIFICATION_NOTIFY_ENABLED": "0"},
            )

        self.assertEqual(response, {"success": True})
        persist.assert_called_once_with({
            "AGENT_DOWNLOAD_VERIFICATION_NOTIFY_ENABLED": "0",
        })


if __name__ == "__main__":
    unittest.main()
