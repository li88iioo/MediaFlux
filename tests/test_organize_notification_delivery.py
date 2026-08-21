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
    @staticmethod
    def _completed_stats(media_items: list[dict]) -> dict:
        return {
            "task_id": "task-media",
            "total": len(media_items),
            "moved": len(media_items),
            "metadata_moved": 0,
            "need_confirm": 0,
            "skipped": 0,
            "failed": 0,
            "media_items": media_items,
            "confirmation_groups": [],
        }

    def test_single_media_task_uses_cover_without_extra_media_message(self) -> None:
        delivered: list[tuple[str, str, dict]] = []
        stats = self._completed_stats([{
            "title": "流浪地球 2",
            "year": "2023",
            "media_type": "movie",
            "tmdb_id": "533535",
            "filename": "The.Wandering.Earth.II.2023.mkv",
            "backdrop_path": "/backdrop.jpg",
        }])

        def deliver(key, body, **kwargs):
            delivered.append((key, body, kwargs))
            return True

        with patch(
            "app.modules.organize_notification_outbox.deliver_organize_notification",
            side_effect=deliver,
        ), patch("app.notifier.send_event") as send_event:
            result = Organizer.notify_task_results(
                stats, OrganizeRules(), source_name="1 个源目录", chat_id="100",
            )

        self.assertTrue(result)
        self.assertEqual(len(delivered), 1)
        self.assertEqual(
            delivered[0][2]["image_url"],
            "https://image.tmdb.org/t/p/w780/backdrop.jpg",
        )
        self.assertIn("新片入库", delivered[0][1])
        send_event.assert_not_called()

    def test_multiple_media_task_keeps_single_text_summary(self) -> None:
        delivered: list[dict] = []
        stats = self._completed_stats([
            {
                "title": "电影甲", "year": "2026", "media_type": "movie",
                "tmdb_id": "1", "filename": "A.mkv", "poster_path": "/a.jpg",
            },
            {
                "title": "电影乙", "year": "2026", "media_type": "movie",
                "tmdb_id": "2", "filename": "B.mkv", "poster_path": "/b.jpg",
            },
        ])

        with patch(
            "app.modules.organize_notification_outbox.deliver_organize_notification",
            side_effect=lambda _key, _body, **kwargs: delivered.append(kwargs) or True,
        ):
            result = Organizer.notify_task_results(
                stats, OrganizeRules(), source_name="2 个源目录", chat_id="100",
            )

        self.assertTrue(result)
        self.assertEqual(delivered, [{"chat_id": "100"}])

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
