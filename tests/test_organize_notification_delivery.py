from __future__ import annotations

from unittest.mock import patch

from app.modules.organize import Organizer, OrganizeRules
from app.notifier import TelegramSendResult
from tests.support import IsolatedDatabaseTestCase


def _stats_with_confirmation() -> dict:
    return {
        "total": 1,
        "moved": 0,
        "metadata_moved": 0,
        "need_confirm": 1,
        "skipped": 0,
        "failed": 0,
        "media_items": [],
        "confirmation_groups": [{
            "identity": "待确认剧集",
            "directory": "Season 01",
            "source_name": "来源目录",
            "files": [{"file_id": "file-1", "name": "Show - 01.mkv"}],
            "candidates": [{
                "tmdb_id": "1",
                "title": "候选剧集",
                "media_type": "tv",
                "score": 0.9,
            }],
        }],
    }


class OrganizeNotificationDeliveryTests(IsolatedDatabaseTestCase):
    def test_confirmation_card_delivery_failure_keeps_terminal_fallback_visible(self) -> None:
        with patch(
            "app.notifier.send_result", return_value=TelegramSendResult(ok=True)
        ) as send_result, patch(
            "app.notifier.send", return_value=True
        ) as send_text, patch(
            "app.notifier.send_event", return_value=False
        ) as send_event:
            delivered = Organizer.notify_task_results(
                _stats_with_confirmation(),
                OrganizeRules(),
                source_name="来源目录",
                chat_id="100",
            )

        self.assertFalse(delivered)
        send_event.assert_called_once()
        send_result.assert_called_once()
        send_text.assert_called_once()
        self.assertIn("待确认候选未能发送", send_text.call_args.args[0])
        self.assertEqual(send_text.call_args.kwargs["chat_id"], "100")

    def test_confirmation_card_delivery_success_reports_complete_notification(self) -> None:
        with patch(
            "app.notifier.send_result", return_value=TelegramSendResult(ok=True)
        ) as send_result, patch(
            "app.notifier.send", return_value=True
        ) as send_text, patch(
            "app.notifier.send_event", return_value=True
        ) as send_event:
            delivered = Organizer.notify_task_results(
                _stats_with_confirmation(),
                OrganizeRules(),
                source_name="来源目录",
                chat_id="100",
            )

        self.assertTrue(delivered)
        send_result.assert_called_once()
        send_text.assert_not_called()
        send_event.assert_called_once()
