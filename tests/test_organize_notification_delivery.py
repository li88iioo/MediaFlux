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

    def test_no_candidate_pending_items_do_not_promise_missing_cards(self) -> None:
        stats = {
            "task_id": "task-no-candidates",
            "total": 2,
            "moved": 0,
            "metadata_moved": 0,
            "need_confirm": 2,
            "skipped": 0,
            "failed": 0,
            "media_items": [],
            "confirmations": ["TMDB 无搜索结果"],
            "confirmation_groups": [],
        }
        delivered: list[str] = []
        with patch(
            "app.modules.organize_notification_outbox.deliver_organize_notification",
            side_effect=lambda _key, body, **_kwargs: delivered.append(body) or True,
        ), patch("app.notifier.send_event") as send_event:
            result = Organizer.notify_task_results(
                stats, OrganizeRules(), source_name="1 个源目录", chat_id="100",
            )

        self.assertTrue(result)
        send_event.assert_not_called()
        self.assertEqual(len(delivered), 1)
        self.assertIn("待确认 2 个", delivered[0])
        self.assertIn("本轮没有可安全操作的 Telegram 处理卡", delivered[0])
        self.assertIn("Web 待确认队列", delivered[0])
        self.assertNotIn("下方将发送", delivered[0])

    def test_confirmation_groups_only_need_a_safe_file_snapshot_for_terminal_skip(self) -> None:
        cases = (
            (
                "missing-candidates",
                {
                    "files": [{"file_id": "file-1", "name": "Show.mkv"}],
                    "candidates": [],
                },
                True,
            ),
            (
                "missing-files",
                {
                    "files": [],
                    "candidates": [{
                        "tmdb_id": "1", "media_type": "tv", "title": "Show",
                    }],
                },
                False,
            ),
            (
                "invalid-candidate",
                {
                    "files": [{"file_id": "file-1", "name": "Show.mkv"}],
                    "candidates": [{
                        "tmdb_id": "", "media_type": "tv", "title": "Show",
                    }],
                },
                True,
            ),
            (
                "malformed-file",
                {
                    "files": [{}],
                    "candidates": [{
                        "tmdb_id": "1", "media_type": "tv", "title": "Show",
                    }],
                },
                False,
            ),
            (
                "mixed-invalid-file",
                {
                    "files": [
                        {"file_id": "file-1", "name": "Show.mkv"},
                        "invalid",
                    ],
                    "candidates": [{
                        "tmdb_id": "1", "media_type": "tv", "title": "Show",
                    }],
                },
                False,
            ),
        )
        for task_id, group, sends_skip_card in cases:
            with self.subTest(task_id=task_id):
                delivered: list[str] = []
                stats = {
                    "task_id": f"task-{task_id}",
                    "total": 1,
                    "moved": 0,
                    "metadata_moved": 0,
                    "need_confirm": 1,
                    "skipped": 0,
                    "failed": 0,
                    "media_items": [],
                    "confirmation_groups": [group],
                }
                with patch(
                    "app.modules.organize_notification_outbox.deliver_organize_notification",
                    side_effect=lambda _key, body, **_kwargs: delivered.append(body) or True,
                ), patch("app.notifier.send_event", return_value=True) as send_event:
                    result = Organizer.notify_task_results(
                        stats, OrganizeRules(), source_name="来源目录", chat_id="100",
                    )

                self.assertTrue(result)
                self.assertEqual(len(delivered), 1)
                if sends_skip_card:
                    send_event.assert_called_once()
                    self.assertIn("下方将发送 1 张处理卡", delivered[0])
                    self.assertIn("1 组暂无可用元数据", delivered[0])
                    self.assertIn("Telegram 直接跳过", delivered[0])
                else:
                    send_event.assert_not_called()
                    self.assertIn("本轮没有可安全操作的 Telegram 处理卡", delivered[0])
                    self.assertNotIn("下方将发送", delivered[0])

    def test_mixed_pending_items_only_promise_actionable_candidate_cards(self) -> None:
        stats = _stats_with_confirmation()
        stats.update({
            "task_id": "task-mixed-candidates",
            "total": 2,
            "need_confirm": 2,
            "confirmations": ["TMDB 无搜索结果"],
        })
        delivered: list[str] = []
        with patch(
            "app.modules.organize_notification_outbox.deliver_organize_notification",
            side_effect=lambda _key, body, **_kwargs: delivered.append(body) or True,
        ), patch("app.notifier.send_event", return_value=True) as send_event:
            result = Organizer.notify_task_results(
                stats, OrganizeRules(), source_name="来源目录", chat_id="100",
            )

        self.assertTrue(result)
        send_event.assert_called_once()
        self.assertEqual(len(delivered), 1)
        self.assertIn("待确认 2 个", delivered[0])
        self.assertIn("1 组可直接选择识别候选", delivered[0])
        self.assertIn("1 个缺少安全文件快照", delivered[0])

    def test_multiple_files_in_one_confirmation_group_explains_single_card(self) -> None:
        stats = _stats_with_confirmation()
        stats.update({
            "task_id": "task-grouped-candidates",
            "total": 2,
            "need_confirm": 2,
        })
        stats["confirmation_groups"][0]["files"] = [
            {"file_id": "file-1", "name": "Show - 01.mkv"},
            {"file_id": "file-2", "name": "Show - 02.mkv"},
        ]
        delivered: list[str] = []
        with patch(
            "app.modules.organize_notification_outbox.deliver_organize_notification",
            side_effect=lambda _key, body, **_kwargs: delivered.append(body) or True,
        ), patch("app.notifier.send_event", return_value=True) as send_event:
            result = Organizer.notify_task_results(
                stats, OrganizeRules(), source_name="来源目录", chat_id="100",
            )

        self.assertTrue(result)
        send_event.assert_called_once()
        self.assertEqual(len(delivered), 1)
        self.assertIn("待确认 2 个文件", delivered[0])
        self.assertIn("已按媒体合并为 1 组", delivered[0])
        self.assertIn("下方将发送 1 张处理卡", delivered[0])

    def test_confirmation_only_delivery_skips_task_summary(self) -> None:
        with patch(
            "app.modules.organize_notification_outbox.deliver_organize_notification",
        ) as deliver_summary, patch(
            "app.notifier.send_event", return_value=True,
        ) as send_event:
            delivered = Organizer.notify_task_confirmations(
                _stats_with_confirmation(),
                OrganizeRules(),
                source_name="来源目录",
                chat_id="100",
            )

        self.assertTrue(delivered)
        deliver_summary.assert_not_called()
        send_event.assert_called_once()
        event = send_event.call_args.args[0]
        self.assertEqual(event.title, "⚠️ 待确认媒体 1/1")
        self.assertTrue(event.actions)

    def test_confirmation_only_delivery_is_noop_without_actionable_groups(self) -> None:
        stats = self._completed_stats([])
        with patch("app.notifier.send_event") as send_event:
            delivered = Organizer.notify_task_confirmations(
                stats,
                OrganizeRules(),
                source_name="来源目录",
                chat_id="100",
            )

        self.assertTrue(delivered)
        send_event.assert_not_called()

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
