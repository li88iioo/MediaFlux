"""Agent 下载后媒体库复核通知的安全投影测试。"""
from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.modules.agent_download_verification_notifications import (
    build_download_verification_event,
    dump_download_verification_payload,
    load_download_verification_payload,
    notify_download_verification_terminal,
)
from app.modules.telegram_notification_center import NotificationPublishResult
from app.notifier import render_event
from app.routes.api import save_config


class DownloadVerificationNotificationTests(unittest.TestCase):
    def test_visible_event_contains_only_safe_fixed_fields(self):
        event = build_download_verification_event(
            title="The <Show>",
            season=2,
            episode=3,
            status="visible",
            result="visible",
            attempts=2,
        )

        rendered = render_event(event)
        self.assertEqual(event.layout, "relaxed")
        self.assertIn("Agent 媒体库复核完成", rendered)
        self.assertIn("- <b>🎬 目标媒体：</b> The &lt;Show&gt;", rendered)
        self.assertIn("S02E03\n\n- <b>🔍 复核结果：</b>", rendered)
        self.assertIn("The &lt;Show&gt;", rendered)
        self.assertIn("S02E03", rendered)
        self.assertIn("已在媒体库中可见", rendered)
        for forbidden in ("request_id", "magnet:", "/volume/", "token", "tmdb_id"):
            self.assertNotIn(forbidden, rendered.lower())

    def test_attention_event_uses_fixed_reason_and_safe_fallback(self):
        event = build_download_verification_event(
            title="\x00unsafe",
            season=1,
            episode=4,
            status="attention",
            result="private backend error",
            attempts=5,
        )

        rendered = render_event(event)
        self.assertIn("Agent 媒体库复核需要处理", rendered)
        self.assertIn("未命名剧集", rendered)
        self.assertIn("无法得出可靠结论", rendered)
        self.assertNotIn("private backend error", rendered)

    def test_sensitive_title_pattern_is_not_rendered_or_logged(self):
        event = build_download_verification_event(
            title="https://private.invalid/file?token=SECRET",
            season=1,
            episode=4,
            status="attention",
            result="missing",
            attempts=2,
        )

        rendered = render_event(event)
        self.assertIn("未命名剧集", rendered)
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
        with patch(
            "app.modules.agent_download_verification_notifications.config.get_bool",
            return_value=False,
        ), patch(
            "app.modules.telegram_notification_center.publish_notification_event"
        ) as sender:
            sent = notify_download_verification_terminal(
                owner="tg:v1:100\x1f200",
                chat_id="100",
                title="The Show",
                season=2,
                episode=3,
                status="visible",
                result="visible",
                attempts=1,
            )

        self.assertFalse(sent)
        sender.assert_not_called()

    def test_enabled_notification_calls_sender(self):
        with patch.dict(
            os.environ,
            {
                "AGENT_ENABLED": "1",
                "TG_AGENT_ENABLED": "1",
                "TG_CHAT_ID": "100",
                "TG_AGENT_ALLOWED_USER_IDS": "200",
            },
            clear=False,
        ), patch(
            "app.modules.agent_download_verification_notifications.config.get_bool",
            return_value=True,
        ), patch(
            "app.modules.telegram_notification_center.publish_notification_event",
            return_value=NotificationPublishResult(True, delivered=True, status="sent"),
        ) as publisher:
            sent = notify_download_verification_terminal(
                owner="tg:v1:100\x1f200",
                chat_id="100",
                title="The Show",
                season=2,
                episode=3,
                status="visible",
                result="visible",
                attempts=1,
            )

        self.assertTrue(sent)
        publisher.assert_called_once()
        logical_key = publisher.call_args.args[0]
        self.assertTrue(logical_key.startswith("agent-download-verification:"))
        self.assertLess(len(logical_key), 128)
        self.assertNotIn("tg:v1", logical_key)
        self.assertNotIn("The Show", logical_key)
        self.assertNotIn("title", logical_key)
        self.assertEqual(publisher.call_args.kwargs["chat_id"], "100")
        self.assertEqual(
            publisher.call_args.args[1].fields[0],
            ("目标媒体", "The Show"),
        )

    def test_revoked_route_never_calls_sender(self):
        base = {
            "AGENT_ENABLED": "1",
            "TG_AGENT_ENABLED": "1",
            "TG_CHAT_ID": "100",
            "TG_AGENT_ALLOWED_USER_IDS": "200",
        }
        for key, value in (
            ("AGENT_ENABLED", "0"),
            ("TG_AGENT_ENABLED", "0"),
            ("TG_CHAT_ID", "999"),
            ("TG_AGENT_ALLOWED_USER_IDS", "201"),
        ):
            with self.subTest(key=key), patch.dict(
                os.environ,
                {**base, key: value},
                clear=False,
            ), patch(
                "app.modules.telegram_notification_center.publish_notification_event"
            ) as sender:
                sent = notify_download_verification_terminal(
                    owner="tg:v1:100\x1f200",
                    chat_id="100",
                    title="The Show",
                    season=2,
                    episode=3,
                    status="visible",
                    result="visible",
                    attempts=1,
                )

            self.assertFalse(sent)
            sender.assert_not_called()

    def test_invalid_or_missing_route_never_uses_global_sender(self):
        with patch(
            "app.modules.agent_download_verification_notifications.config.get_bool",
            return_value=True,
        ), patch(
            "app.modules.telegram_notification_center.publish_notification_event"
        ) as sender:
            sent = notify_download_verification_terminal(
                owner="web:v1:abc",
                chat_id="",
                title="The Show",
                season=2,
                episode=3,
                status="visible",
                result="visible",
                attempts=1,
            )
        self.assertFalse(sent)
        sender.assert_not_called()

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
                request,
                {"AGENT_DOWNLOAD_VERIFICATION_NOTIFY_ENABLED": "0"},
            )

        self.assertEqual(response, {"success": True})
        persist.assert_called_once_with({
            "AGENT_DOWNLOAD_VERIFICATION_NOTIFY_ENABLED": "0",
        })


if __name__ == "__main__":
    unittest.main()
