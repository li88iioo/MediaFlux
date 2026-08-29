"""统一 Telegram 通知中心的幂等、线程更新与按钮保留契约。"""
from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import database as db
from app.modules.telegram_notification_center import (
    deserialize_notification_event,
    drain_telegram_notifications,
    notification_thread_event_key,
    publish_notification_event,
    publish_notification_thread,
    serialize_notification_event,
)
from app.modules.telegram_notification_policy import (
    NotificationImportance,
    NotificationTopic,
)
from app.notifier import NotificationAction, NotificationEvent, TelegramSendResult
from app.repositories.telegram_notifications import get_notification
from app.routes.api import save_config
from tests.support import IsolatedDatabaseTestCase


class TelegramNotificationCenterTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM telegram_notification_outbox")

    def test_round_trip_keeps_actions_and_rendering_flags(self) -> None:
        event = NotificationEvent(
            "待确认",
            fields=(("媒体", "测试"),),
            lines=("候选 1",),
            footer="请选择",
            actions=(NotificationAction("确认", "orgc:token:0"),),
            layout="relaxed",
            field_emojis=False,
        )
        restored = deserialize_notification_event(serialize_notification_event(event))
        self.assertEqual(restored, event)
        self.assertEqual(restored.actions[0].callback_data, "orgc:token:0")

    @patch("app.modules.telegram_notification_center.send_event_result")
    def test_one_shot_event_is_idempotent(self, sender) -> None:
        sender.return_value = TelegramSendResult(ok=True, message_id=81)
        event = NotificationEvent("订阅更新")
        first = publish_notification_event(
            "subscription:1:missing", event,
            topic=NotificationTopic.MEDIA_SUBSCRIPTION, chat_id="100",
        )
        second = publish_notification_event(
            "subscription:1:missing", event,
            topic=NotificationTopic.MEDIA_SUBSCRIPTION, chat_id="100",
        )
        self.assertTrue(first.delivered)
        self.assertTrue(second.delivered)
        sender.assert_called_once()

    @patch("app.modules.telegram_notification_center.edit_event_result")
    @patch("app.modules.telegram_notification_center.send_event_result")
    def test_thread_updates_same_message_and_preserves_buttons(self, sender, editor) -> None:
        sender.return_value = TelegramSendResult(ok=True, message_id=42)
        editor.return_value = TelegramSendResult(ok=True, message_id=42)
        initial = NotificationEvent(
            "待确认",
            actions=(NotificationAction("选择", "orgc:abc:0"),),
        )
        terminal = NotificationEvent("整理完成")

        first = publish_notification_thread(
            "confirmation:abc", initial,
            topic=NotificationTopic.CONFIRMATION,
            importance=NotificationImportance.ACTION,
            chat_id="100",
        )
        second = publish_notification_thread(
            "confirmation:abc", terminal,
            topic=NotificationTopic.CONFIRMATION,
            importance=NotificationImportance.RESULT,
            chat_id="100",
        )

        self.assertTrue(first.delivered)
        self.assertTrue(second.delivered)
        sender.assert_called_once()
        editor.assert_called_once()
        self.assertEqual(editor.call_args.kwargs["message_id"], 42)
        key = notification_thread_event_key(
            "confirmation:abc", topic=NotificationTopic.CONFIRMATION, chat_id="100",
        )
        row = get_notification(key)
        self.assertEqual(row["message_id"], 42)
        self.assertEqual(row["revision"], 2)
        self.assertEqual(row["delivered_revision"], 2)
        self.assertEqual(row["status"], "sent")

    def test_global_switch_rejects_proactive_event_without_creating_outbox(self) -> None:
        with patch(
            "app.modules.telegram_notification_policy.config.get_bool",
            return_value=False,
        ):
            outcome = publish_notification_event(
                "disabled-event",
                NotificationEvent("不应发送"),
                topic=NotificationTopic.SYSTEM,
                chat_id="100",
            )
        self.assertFalse(outcome.accepted)
        self.assertEqual(outcome.status, "disabled")
        with db.get_conn() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM telegram_notification_outbox"
            ).fetchone()[0]
        self.assertEqual(total, 0)

    @patch("app.modules.telegram_notification_center.send_event_result")
    def test_dispatch_rechecks_level_and_suppresses_downgraded_detail(self, sender) -> None:
        with patch(
            "app.modules.telegram_notification_policy.config.get_bool",
            return_value=True,
        ), patch(
            "app.modules.telegram_notification_policy.config.get",
            return_value="detailed",
        ):
            outcome = publish_notification_event(
                "detail-event",
                NotificationEvent("技术明细"),
                topic=NotificationTopic.STRM,
                importance=NotificationImportance.DETAIL,
                chat_id="100",
                deliver_now=False,
            )
        self.assertTrue(outcome.queued)
        with patch(
            "app.modules.telegram_notification_policy.config.get_bool",
            return_value=True,
        ), patch(
            "app.modules.telegram_notification_policy.config.get",
            return_value="standard",
        ):
            self.assertTrue(drain_telegram_notifications(event_key=outcome.event_key))
        sender.assert_not_called()
        self.assertEqual(get_notification(outcome.event_key)["status"], "suppressed")

    @patch("app.modules.telegram_notification_center.send_event_result")
    def test_retry_keeps_single_outbox_row(self, sender) -> None:
        sender.side_effect = [
            TelegramSendResult(ok=False, error="temporary", status_code=503),
            TelegramSendResult(ok=True, message_id=7),
        ]
        outcome = publish_notification_thread(
            "download:9", NotificationEvent("处理中"),
            topic=NotificationTopic.DOWNLOAD, chat_id="100",
            deliver_now=True,
        )
        self.assertTrue(outcome.accepted)
        self.assertTrue(outcome.queued)
        key = outcome.event_key
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE telegram_notification_outbox SET next_attempt_at=? WHERE event_key=?",
                (db.now(), key),
            )
        self.assertTrue(drain_telegram_notifications(event_key=key))
        row = get_notification(key)
        self.assertEqual(row["status"], "sent")
        self.assertEqual(row["message_id"], 7)

    @patch("app.modules.telegram_notification_center.edit_event_result")
    @patch("app.modules.telegram_notification_center.send_event_result")
    def test_confirmation_thread_keeps_token_and_receives_downstream_result(
        self, sender, editor,
    ) -> None:
        from app.modules.organize_confirmations import publish_confirmation_event
        from app.modules.scheduler import _publish_linked_notification_threads

        sender.return_value = TelegramSendResult(ok=True, message_id=73)
        editor.return_value = TelegramSendResult(ok=True, message_id=73)
        token = "confirm-token"
        db.create_organize_confirmation(
            token=token,
            fingerprint="fingerprint",
            chat_id="100",
            source_name="光鸭云盘",
            directory_path="/动漫",
            payload={"files": [{"file_id": "1"}]},
            expires_at="2099-01-01 00:00:00",
        )
        initial = NotificationEvent(
            "待确认",
            fields=(("媒体", "测试动画"),),
            actions=(NotificationAction("确认", f"orgc:{token}:0"),),
        )
        self.assertTrue(publish_confirmation_event(
            initial, chat_id="100", token=token,
        ))
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE organize_confirmations SET status='completed' WHERE token=?",
                (token,),
            )

        accepted = _publish_linked_notification_threads(
            {"notification_threads": [{
                "topic": "confirmation",
                "thread_key": f"confirmation:{token}",
                "token": token,
                "chat_id": "100",
                "topic_enabled": True,
            }]},
            strm_status="完成",
            media_refresh="Jellyfin 完成",
        )

        self.assertTrue(accepted)
        sender.assert_called_once()
        editor.assert_called_once()
        final_event = editor.call_args.args[0]
        self.assertIn(("STRM", "完成"), final_event.fields)
        self.assertIn(("媒体库", "Jellyfin 完成"), final_event.fields)
        self.assertEqual(final_event.actions, ())

    @patch("app.modules.telegram_notification_center.edit_event_result")
    @patch("app.modules.telegram_notification_center.send_event_result")
    def test_download_pipeline_collapses_into_one_message(self, sender, editor) -> None:
        from app.modules.telegram_download_lifecycle import publish_download_lifecycle

        sender.return_value = TelegramSendResult(ok=True, message_id=99)
        editor.return_value = TelegramSendResult(ok=True, message_id=99)
        request_id, _ = db.create_download_request(
            "tg-lifecycle", "magnet", title="测试剧", chat_id="100",
        )
        db.update_download_request(
            request_id, status="completed", qb_status="completed",
            organize_status="running", strm_status="pending",
        )
        first = publish_download_lifecycle(request_id, stats={
            "media_items": [{
                "title": "测试剧", "year": "2026", "media_type": "tv",
                "season": 1, "episode": 3,
            }],
        })
        db.update_download_request(
            request_id, organize_status="completed", strm_status="completed",
        )
        second = publish_download_lifecycle(
            request_id, media_refresh="Jellyfin 已刷新",
            verification_status="visible", verification_result="目标剧集已可见",
        )
        self.assertTrue(first.delivered)
        self.assertTrue(second.delivered)
        sender.assert_called_once()
        editor.assert_called_once()
        final_event = editor.call_args.args[0]
        self.assertIn("下载与入库完成", final_event.title)
        self.assertIn(("媒体库", "Jellyfin 已刷新"), final_event.fields)
        self.assertIn(("入库复核", "目标剧集已可见"), final_event.fields)
        self.assertTrue(any("S01E03" in line for line in final_event.lines))


class TelegramNotificationSettingsTests(unittest.TestCase):
    @staticmethod
    def _request():
        return SimpleNamespace(
            session={"logged_in": True},
            app=SimpleNamespace(state=SimpleNamespace(
                background_services_enabled=False,
                media_proxy_manager=None,
            )),
        )

    def test_settings_expose_stable_global_switch_and_level_control(self) -> None:
        template = Path("app/templates/settings.html").read_text(encoding="utf-8")
        script = Path("app/static/js/settings.js").read_text(encoding="utf-8")
        self.assertEqual(template.count('data-key="TG_NOTIFICATION_ENABLED"'), 1)
        self.assertEqual(template.count('data-key="TG_NOTIFICATION_LEVEL"'), 1)
        for value in ("essential", "standard", "detailed"):
            self.assertIn(f'<option value="{value}"', template)
        self.assertIn("TG_NOTIFICATION_ENABLED:'1'", script)
        self.assertIn("TG_NOTIFICATION_LEVEL:'standard'", script)

    def test_config_api_normalizes_switch_and_rejects_unknown_level(self) -> None:
        with patch(
            "app.routes.api.config.get", side_effect=lambda _key, default="": default,
        ), patch("app.routes.api.config.set_and_save") as persist, patch(
            "app.services.clear_dashboard_cache"
        ):
            response = save_config(self._request(), {
                "TG_NOTIFICATION_ENABLED": "true",
                "TG_NOTIFICATION_LEVEL": "DETAILED",
            })
        self.assertEqual(response, {"success": True})
        persist.assert_called_once_with({
            "TG_NOTIFICATION_ENABLED": "1",
            "TG_NOTIFICATION_LEVEL": "detailed",
        })

        with patch("app.routes.api.config.set_and_save") as rejected:
            response = save_config(
                self._request(), {"TG_NOTIFICATION_LEVEL": "verbose"}
            )
        self.assertEqual(response.status_code, 400)
        rejected.assert_not_called()


if __name__ == "__main__":
    unittest.main()
