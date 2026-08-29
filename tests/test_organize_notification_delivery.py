from __future__ import annotations

from unittest.mock import patch

from app.modules.organize import Organizer, OrganizeRules
from app.modules.telegram_notification_center import NotificationPublishResult
from app.modules.telegram_organize_lifecycle import build_organize_lifecycle_event
from tests.support import IsolatedDatabaseTestCase


def _stats_with_confirmation() -> dict:
    return {
        "task_id": "task-confirmation",
        "total": 1,
        "moved": 0,
        "metadata_moved": 0,
        "need_confirm": 1,
        "skipped": 0,
        "failed": 0,
        "media_items": [],
        "confirmation_groups": [{
            "identity": "待确认剧集",
            "directory": "Season 1",
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


def _accepted() -> NotificationPublishResult:
    return NotificationPublishResult(True, delivered=True, status="sent")


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

    @staticmethod
    def _capture_lifecycle(stats: dict, events: list, **kwargs):
        events.append(build_organize_lifecycle_event(
            stats,
            source_name=str(kwargs.get("source_name") or ""),
            strm_status=str(kwargs.get("strm_status") or ""),
            media_refresh=str(kwargs.get("media_refresh") or ""),
        ))
        return _accepted()

    def test_directory_and_post_action_summary_reuse_one_thread(self) -> None:
        calls = []
        stats = self._completed_stats([{
            "title": "超能立方",
            "year": "2025",
            "media_type": "tv",
            "tmdb_id": "279182",
            "season": 1,
            "episode": 1,
        }])
        stats.pop("task_id", None)
        rules = OrganizeRules(link_strm=True)

        with patch(
            "app.modules.telegram_organize_lifecycle.publish_notification_thread",
            side_effect=lambda key, event, **kwargs: calls.append(
                (key, event, kwargs)
            ) or _accepted(),
        ):
            Organizer.notify_directory_results(
                stats, rules, source_name="超能立方", chat_id="100",
            )
            stats["strm"] = {"ok": True, "message": "STRM 同步任务已启动"}
            Organizer._notify_result(
                stats, rules, source_name="超能立方", chat_id="100",
            )

        self.assertEqual(len(calls), 2)
        self.assertTrue(str(stats["task_id"]).startswith("directory-"))
        self.assertEqual(calls[0][0], calls[1][0])
        self.assertNotIn("legacy-", calls[1][0])
        self.assertIn(("STRM", "等待后处理"), calls[0][1].fields)
        self.assertIn(("STRM", "已排队"), calls[1][1].fields)

    def test_skipped_directory_never_claims_strm_is_waiting(self) -> None:
        calls = []
        stats = self._completed_stats([])
        stats.update({"total": 1, "moved": 0, "skipped": 1})

        with patch(
            "app.modules.telegram_organize_lifecycle.publish_notification_thread",
            side_effect=lambda key, event, **kwargs: calls.append(event) or _accepted(),
        ):
            Organizer.notify_directory_results(
                stats, OrganizeRules(link_strm=True), source_name="暗芝居", chat_id="100",
            )

        self.assertEqual(len(calls), 1)
        self.assertIn(("STRM", "未启用或无变更"), calls[0].fields)
        self.assertIn(("媒体库", "未触发"), calls[0].fields)

    def test_single_media_task_uses_one_transaction_message(self) -> None:
        events = []
        stats = self._completed_stats([{
            "title": "流浪地球 2",
            "year": "2023",
            "media_type": "movie",
            "tmdb_id": "533535",
            "filename": "The.Wandering.Earth.II.2023.mkv",
            "backdrop_path": "/backdrop.jpg",
        }])
        with patch(
            "app.modules.telegram_organize_lifecycle.publish_organize_lifecycle",
            side_effect=lambda task_id, payload, **kwargs: self._capture_lifecycle(
                payload, events, **kwargs,
            ),
        ) as lifecycle, patch(
            "app.modules.organize_confirmations.publish_confirmation_event"
        ) as confirmation:
            result = Organizer.notify_task_results(
                stats, OrganizeRules(), source_name="1 个源目录", chat_id="100",
            )
        self.assertTrue(result)
        lifecycle.assert_called_once()
        confirmation.assert_not_called()
        self.assertEqual(len(events), 1)
        self.assertTrue(any("流浪地球 2" in line for line in events[0].lines))

    def test_multiple_media_task_keeps_single_compact_summary(self) -> None:
        events = []
        stats = self._completed_stats([
            {"title": "电影甲", "year": "2026", "media_type": "movie"},
            {"title": "电影乙", "year": "2026", "media_type": "movie"},
        ])
        with patch(
            "app.modules.telegram_organize_lifecycle.publish_organize_lifecycle",
            side_effect=lambda task_id, payload, **kwargs: self._capture_lifecycle(
                payload, events, **kwargs,
            ),
        ):
            self.assertTrue(Organizer.notify_task_results(
                stats, OrganizeRules(), source_name="2 个源目录", chat_id="100",
            ))
        self.assertEqual(len(events), 1)
        self.assertEqual(len(events[0].lines), 2)

    def test_no_candidate_pending_items_do_not_promise_missing_cards(self) -> None:
        events = []
        stats = {
            "task_id": "task-no-candidates", "total": 2, "moved": 0,
            "metadata_moved": 0, "need_confirm": 2, "skipped": 0,
            "failed": 0, "media_items": [],
            "confirmations": ["TMDB 无搜索结果"], "confirmation_groups": [],
        }
        with patch(
            "app.modules.telegram_organize_lifecycle.publish_organize_lifecycle",
            side_effect=lambda task_id, payload, **kwargs: self._capture_lifecycle(
                payload, events, **kwargs,
            ),
        ), patch(
            "app.modules.organize_confirmations.publish_confirmation_event"
        ) as confirmation:
            self.assertTrue(Organizer.notify_task_results(
                stats, OrganizeRules(), source_name="1 个源目录", chat_id="100",
            ))
        confirmation.assert_not_called()
        self.assertIn(("人工确认", "2 个暂无候选"), events[0].fields)
        self.assertIn("暂无可用候选", events[0].footer)
        self.assertNotIn("按钮卡发送", events[0].footer)

    def test_confirmation_groups_require_safe_snapshot_but_keep_skip_buttons(self) -> None:
        cases = (
            (
                "missing-candidates",
                {"files": [{"file_id": "file-1", "name": "Show.mkv"}], "candidates": []},
                True,
            ),
            (
                "missing-files",
                {"files": [], "candidates": [{"tmdb_id": "1", "media_type": "tv"}]},
                False,
            ),
            (
                "invalid-candidate",
                {
                    "files": [{"file_id": "file-1", "name": "Show.mkv"}],
                    "candidates": [{"tmdb_id": "", "media_type": "tv"}],
                },
                True,
            ),
            (
                "malformed-file",
                {"files": [{}], "candidates": [{"tmdb_id": "1", "media_type": "tv"}]},
                False,
            ),
            (
                "mixed-invalid-file",
                {
                    "files": [{"file_id": "file-1", "name": "Show.mkv"}, "bad"],
                    "candidates": [{"tmdb_id": "1", "media_type": "tv"}],
                },
                False,
            ),
        )
        for task_id, group, sends_skip_card in cases:
            with self.subTest(task_id=task_id):
                events = []
                stats = {
                    "task_id": f"task-{task_id}", "total": 1,
                    "moved": 0, "metadata_moved": 0, "need_confirm": 1,
                    "skipped": 0, "failed": 0, "media_items": [],
                    "confirmation_groups": [group],
                }
                with patch(
                    "app.modules.telegram_organize_lifecycle.publish_organize_lifecycle",
                    side_effect=lambda task_id, payload, **kwargs: self._capture_lifecycle(
                        payload, events, **kwargs,
                    ),
                ), patch(
                    "app.modules.organize_confirmations.publish_confirmation_event",
                    return_value=True,
                ) as confirmation:
                    self.assertTrue(Organizer.notify_task_results(
                        stats, OrganizeRules(), source_name="来源目录", chat_id="100",
                    ))
                if sends_skip_card:
                    confirmation.assert_called_once()
                    card = confirmation.call_args.args[0]
                    self.assertTrue(any(
                        action.callback_data.endswith(":skip") for action in card.actions
                    ))
                    self.assertIn(("人工确认", "1 个文件 / 1 组跳过卡"), events[0].fields)
                else:
                    confirmation.assert_not_called()
                    self.assertIn(("人工确认", "1 个暂无候选"), events[0].fields)

    def test_mixed_pending_items_only_report_real_actionable_cards(self) -> None:
        events = []
        stats = _stats_with_confirmation()
        stats.update({"total": 2, "need_confirm": 2})
        with patch(
            "app.modules.telegram_organize_lifecycle.publish_organize_lifecycle",
            side_effect=lambda task_id, payload, **kwargs: self._capture_lifecycle(
                payload, events, **kwargs,
            ),
        ), patch(
            "app.modules.organize_confirmations.publish_confirmation_event",
            return_value=True,
        ) as confirmation:
            self.assertTrue(Organizer.notify_task_results(
                stats, OrganizeRules(), source_name="来源目录", chat_id="100",
            ))
        confirmation.assert_called_once()
        self.assertIn(("人工确认", "1 个文件 / 1 组候选卡 · 1 个暂无候选"), events[0].fields)

    def test_multiple_files_in_one_group_explains_single_card(self) -> None:
        events = []
        stats = _stats_with_confirmation()
        stats.update({"total": 2, "need_confirm": 2})
        stats["confirmation_groups"][0]["files"] = [
            {"file_id": "file-1", "name": "Show - 01.mkv"},
            {"file_id": "file-2", "name": "Show - 02.mkv"},
        ]
        with patch(
            "app.modules.telegram_organize_lifecycle.publish_organize_lifecycle",
            side_effect=lambda task_id, payload, **kwargs: self._capture_lifecycle(
                payload, events, **kwargs,
            ),
        ), patch(
            "app.modules.organize_confirmations.publish_confirmation_event",
            return_value=True,
        ) as confirmation:
            self.assertTrue(Organizer.notify_task_results(
                stats, OrganizeRules(), source_name="来源目录", chat_id="100",
            ))
        confirmation.assert_called_once()
        self.assertIn(("人工确认", "2 个文件 / 1 组候选卡"), events[0].fields)

    def test_confirmation_only_delivery_skips_task_summary(self) -> None:
        with patch(
            "app.modules.telegram_organize_lifecycle.publish_organize_lifecycle"
        ) as lifecycle, patch(
            "app.modules.organize_confirmations.publish_confirmation_event",
            return_value=True,
        ) as confirmation:
            delivered = Organizer.notify_task_confirmations(
                _stats_with_confirmation(), OrganizeRules(),
                source_name="来源目录", chat_id="100",
            )
        self.assertTrue(delivered)
        lifecycle.assert_not_called()
        confirmation.assert_called_once()
        event = confirmation.call_args.args[0]
        self.assertEqual(event.title, "⚠️ 待确认媒体 1/1")
        self.assertTrue(event.actions)

    def test_confirmation_only_delivery_is_noop_without_actionable_groups(self) -> None:
        with patch(
            "app.modules.organize_confirmations.publish_confirmation_event"
        ) as confirmation:
            delivered = Organizer.notify_task_confirmations(
                self._completed_stats([]), OrganizeRules(),
                source_name="来源目录", chat_id="100",
            )
        self.assertTrue(delivered)
        confirmation.assert_not_called()

    def test_confirmation_card_acceptance_failure_is_reported_without_duplicate_fallback(self) -> None:
        events = []
        with patch(
            "app.modules.telegram_organize_lifecycle.publish_organize_lifecycle",
            side_effect=lambda task_id, payload, **kwargs: self._capture_lifecycle(
                payload, events, **kwargs,
            ),
        ), patch(
            "app.modules.organize_confirmations.publish_confirmation_event",
            return_value=False,
        ) as confirmation:
            delivered = Organizer.notify_task_results(
                _stats_with_confirmation(), OrganizeRules(),
                source_name="来源目录", chat_id="100",
            )
        self.assertFalse(delivered)
        confirmation.assert_called_once()
        self.assertIn("独立按钮卡", events[0].footer)
        self.assertIn("Web", events[0].footer)

    def test_confirmation_card_acceptance_completes_notification(self) -> None:
        with patch(
            "app.modules.telegram_organize_lifecycle.publish_organize_lifecycle",
            return_value=_accepted(),
        ) as lifecycle, patch(
            "app.modules.organize_confirmations.publish_confirmation_event",
            return_value=True,
        ) as confirmation:
            delivered = Organizer.notify_task_results(
                _stats_with_confirmation(), OrganizeRules(),
                source_name="来源目录", chat_id="100",
            )
        self.assertTrue(delivered)
        lifecycle.assert_called_once()
        confirmation.assert_called_once()
