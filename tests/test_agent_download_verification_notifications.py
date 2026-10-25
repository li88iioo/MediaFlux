"""复核通知必须使用真实请求、当前复核事实和统一下载消息 outbox。"""
from __future__ import annotations

import os
import threading
import unittest
from inspect import Parameter, signature
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from starlette.requests import Request

from app import database as db
from app.modules import telegram_notification_center as center
from app.modules.agent_download_verification_notifications import (
    dump_download_verification_payload,
    load_download_verification_payload,
    notify_download_verification_terminal_result,
)
from app.modules.telegram_notification_center import (
    get_notification_thread_snapshot,
    notification_thread_event_key,
)
from app.modules.telegram_notification_policy import NotificationTopic
from app.notifier import TelegramSendResult, render_event
from app.repositories.telegram_notifications import get_notification
from app.routes.api import save_config
from tests.support import IsolatedDatabaseTestCase


class DownloadVerificationNotificationTests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM agent_download_verification_notification_outbox")
            conn.execute("DELETE FROM agent_download_verifications")
            conn.execute("DELETE FROM telegram_notification_outbox")
            conn.execute("DELETE FROM download_requests")
        self.enterContext(patch.dict(os.environ, {
            "AGENT_ENABLED": "1", "TG_AGENT_ENABLED": "1", "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200", "TG_NOTIFICATION_ENABLED": "1",
            "TG_NOTIFICATION_LEVEL": "standard", "AGENT_DOWNLOAD_VERIFICATION_NOTIFY_ENABLED": "1",
        }))
        for target in ("socket.socket.connect", "socket.socket.connect_ex", "socket.create_connection"):
            self.enterContext(patch(target, side_effect=AssertionError("R07 forbids network")))
        self.enterContext(patch.object(center, "_dispatch_stop", threading.Event()))
        self.enterContext(patch.object(center, "wake_telegram_notification_dispatcher"))
        self.sender = self.enterContext(patch.object(
            center, "send_event_result", return_value=TelegramSendResult(True, message_id=73),
        ))
        self.enterContext(patch.object(
            center, "edit_event_result", return_value=TelegramSendResult(True, message_id=73),
        ))

    def _request(self, *, status="visible", result="visible", payload_title="The <Show>"):
        request_id, _ = db.create_download_request(
            self.id(), "magnet", title="The <Show> S02E03", origin="agent", chat_id="100",
        )
        db.update_download_request(
            request_id, targets="qb", status="completed", qb_status="completed",
            local_import_status="completed", strm_status="completed",
        )
        self.assertTrue(db.enqueue_agent_download_verification(
            request_id, title="The <Show>", tmdb_id="123", season=2, episode=3,
            as_of="2026-10-26", owner="tg:v1:100\x1f200", chat_id="100",
        ))
        job = db.claim_due_agent_download_verification()
        assert job is not None
        serialized = dump_download_verification_payload(
            title=payload_title, season=2, episode=3, status=status, result=result, attempts=2,
        )
        payload = load_download_verification_payload(serialized)
        self.assertTrue(db.finish_agent_download_verification(
            request_id, status=payload["status"], result=payload["result"], attempts=payload["attempts"],
            next_check_at=db.now(), expected_lease_generation=job["lease_generation"], payload_json=serialized,
        ))
        return request_id

    def _event(self, request_id):
        snapshot = get_notification_thread_snapshot(
            f"download:{request_id}", topic=NotificationTopic.DOWNLOAD, chat_id="100",
        )
        assert snapshot is not None
        return snapshot.event

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
        payload = load_download_verification_payload(db.list_agent_download_verification_notifications()[0]["payload_json"])
        self.assertEqual(payload["title"], "未命名剧集")
        self.assertEqual(payload["result"], "inconclusive")

    def test_sensitive_title_pattern_is_not_rendered_or_logged(self):
        request_id = self._request(
            status="attention", result="missing", payload_title="https://private.invalid/file?token=SECRET",
        )
        self.assertTrue(notify_download_verification_terminal_result(
            owner="tg:v1:100\x1f200", chat_id="100", request_id=request_id,
        ).ok)

        rendered = render_event(self._event(request_id))
        payload = db.list_agent_download_verification_notifications()[0]["payload_json"]
        self.assertEqual(load_download_verification_payload(payload)["title"], "未命名剧集")
        self.assertNotIn("private.invalid", payload)
        self.assertNotIn("SECRET", payload)
        self.assertNotIn("private.invalid", rendered)
        self.assertNotIn("SECRET", rendered)

    def test_persistent_payload_is_safe_and_strict(self):
        serialized = dump_download_verification_payload(
            title="https://private.invalid/file?token=SECRET",
            season=2,
            episode=3,
            status="visible",
            result="visible",
            attempts=2,
        )
        self.assertNotIn("private.invalid", serialized)
        self.assertNotIn("SECRET", serialized)
        self.assertEqual(load_download_verification_payload(serialized), {
            "title": "未命名剧集",
            "season": 2,
            "episode": 3,
            "status": "visible",
            "result": "visible",
            "attempts": 2,
        })
        with self.assertRaises(ValueError):
            load_download_verification_payload('{"title":"x","secret":"no"}')

    def test_disabled_notification_never_calls_sender(self):
        request_id = self._request()
        with patch.dict(os.environ, {"AGENT_DOWNLOAD_VERIFICATION_NOTIFY_ENABLED": "0"}):
            sent = notify_download_verification_terminal_result(
                owner="tg:v1:100\x1f200", chat_id="100", request_id=request_id,
            )

        self.assertFalse(sent.ok)
        self.sender.assert_not_called()
        self.assertEqual(db.list_agent_download_verification_notifications()[0]["status"], "pending")

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

    def test_revoked_route_never_calls_sender(self):
        request_id = self._request()
        for key, value in (
            ("AGENT_ENABLED", "0"),
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
        from app.modules.agent_download_verification_scheduler import DownloadLibraryVerificationScheduler

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
        self.assertEqual(db.list_agent_download_verification_notifications()[0]["status"], "sent")

    def test_notification_switch_is_accepted_by_config_api(self):
        request = SimpleNamespace(
            session={"logged_in": True},
            app=SimpleNamespace(
                state=SimpleNamespace(
                    background_services_enabled=False,
                    media_proxy_manager=None,
                )
            ),
        )
        with patch("app.routes.api.config.get", return_value=""), patch(
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
