"""Telegram 富媒体通知行为测试。

所有 Telegram 边界均使用内存替身，禁止真实网络发送。
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import Mock, patch

from app import notifier
from tests.support import release_parse_result


class FakeBot:
    def __init__(self, *, photo_error: Exception | None = None,
                 message_error: Exception | None = None) -> None:
        self.photo_error = photo_error
        self.message_error = message_error
        self.message_attempts = 0
        self.photos: list[tuple[str, str, str]] = []
        self.messages: list[tuple[str, str]] = []
        self.edits: list[tuple[str, str, int, object]] = []

    def send_photo(self, chat_id: str, photo: str, caption: str = "") -> None:
        if self.photo_error:
            raise self.photo_error
        self.photos.append((chat_id, photo, caption))

    def send_message(self, chat_id: str, text: str) -> None:
        self.message_attempts += 1
        if self.message_error:
            raise self.message_error
        self.messages.append((chat_id, text))

    def edit_message_text(
        self, text: str, chat_id: str, message_id: int, *, reply_markup=None
    ) -> None:
        if self.message_error:
            raise self.message_error
        self.edits.append((text, chat_id, message_id, reply_markup))


class NotificationSendingTests(unittest.TestCase):
    def setUp(self) -> None:
        notifier.reset()

    def tearDown(self) -> None:
        notifier.reset()

    @staticmethod
    def _install_bot(bot: FakeBot, chat_id: str = "100") -> None:
        notifier._bot = bot
        notifier._chat_id = chat_id
        notifier._initialized = True

    def test_test_mode_denies_real_telebot_initialization_and_transport_by_default(self):
        config = {
            "TG_BOT_TOKEN": "dummy-token-final-fix",
            "TG_CHAT_ID": "999999",
        }
        with patch.dict(
            os.environ,
            {"MEDIAFLUX_TEST_MODE": "1"},
            clear=False,
        ), patch.dict(
            os.environ,
            {"MEDIAFLUX_TEST_ALLOW_TELEGRAM": ""},
            clear=False,
        ), patch.object(
            notifier, "get", side_effect=lambda key, default="": config.get(key, default)
        ), patch("telebot.TeleBot") as constructor:
            result = notifier.send_event(notifier.NotificationEvent("测试通知"))

        self.assertFalse(result)
        constructor.assert_not_called()
        self.assertIsNone(notifier._bot)

    def test_test_mode_explicit_fake_bot_injection_keeps_send_semantics(self):
        bot = FakeBot()
        with patch.dict(os.environ, {"MEDIAFLUX_TEST_MODE": "1"}, clear=False):
            self._install_bot(bot, "test-chat")
            self.assertTrue(notifier.send_event(notifier.NotificationEvent("测试通知")))

        self.assertEqual(bot.messages, [("test-chat", "<b>ℹ️ 测试通知</b>")])

    def test_send_result_preserves_telegram_retry_after(self):
        class TelegramRateLimitError(RuntimeError):
            result_json = {
                "error_code": 429,
                "description": "Too Many Requests: retry later",
                "parameters": {"retry_after": 37},
            }

        bot = FakeBot(message_error=TelegramRateLimitError(
            "https://api.telegram.org/bot123456:secret/sendMessage failed"
        ))
        self._install_bot(bot, "test-chat")

        result = notifier.send_result("测试通知")

        self.assertFalse(result.ok)
        self.assertEqual(result.status_code, 429)
        self.assertEqual(result.retry_after_seconds, 37)
        self.assertEqual(result.error, "Too Many Requests: retry later")
        self.assertFalse(notifier.send("兼容布尔接口"))

    def test_send_result_uses_cover_and_falls_back_to_text(self):
        class TelegramPhotoRejected(RuntimeError):
            result_json = {
                "error_code": 400,
                "description": "Bad Request: failed to get HTTP URL content",
            }

        bot = FakeBot()
        self._install_bot(bot, "test-chat")

        result = notifier.send_result(
            "<b>整理完成</b>", image_url="https://image.example/poster.jpg",
        )

        self.assertTrue(result.ok)
        self.assertEqual(bot.photos, [(
            "test-chat", "https://image.example/poster.jpg", "<b>整理完成</b>",
        )])
        self.assertEqual(bot.messages, [])

        fallback = FakeBot(photo_error=TelegramPhotoRejected("image unavailable"))
        self._install_bot(fallback, "test-chat")
        result = notifier.send_result(
            "<b>整理完成</b>", image_url="https://image.example/missing.jpg",
        )

        self.assertTrue(result.ok)
        self.assertEqual(fallback.messages, [("test-chat", "<b>整理完成</b>")])

    def test_edit_event_updates_existing_message_with_structured_markup(self):
        bot = FakeBot()
        self._install_bot(bot, "configured-chat")
        event = notifier.NotificationEvent(
            "确认整理失败",
            actions=(notifier.NotificationAction("重试", "orgc:token:0"),),
        )

        self.assertTrue(notifier.edit_event(event, chat_id="-100", message_id="77"))
        self.assertEqual(bot.edits[0][1:3], ("-100", 77))
        self.assertEqual(
            bot.edits[0][3].keyboard[0][0].callback_data, "orgc:token:0"
        )

    def test_edit_event_treats_unchanged_message_as_idempotent_success(self):
        bot = FakeBot(message_error=RuntimeError(
            "Bad Request: message is not modified: specified new message content"
        ))
        self._install_bot(bot, "configured-chat")

        self.assertTrue(notifier.edit_event(
            notifier.NotificationEvent("确认整理完成"),
            chat_id="-100",
            message_id="77",
        ))
        self.assertEqual(bot.message_attempts, 0)

    def test_test_mode_can_explicitly_opt_in_to_telebot_initialization(self):
        bot = FakeBot()
        config = {
            "TG_BOT_TOKEN": "dummy-token-final-fix",
            "TG_CHAT_ID": "opt-in-chat",
        }
        with patch.dict(
            os.environ,
            {
                "MEDIAFLUX_TEST_MODE": "1",
                "MEDIAFLUX_TEST_ALLOW_TELEGRAM": "1",
            },
            clear=False,
        ), patch.object(
            notifier, "get", side_effect=lambda key, default="": config.get(key, default)
        ), patch("telebot.TeleBot", return_value=bot) as constructor:
            self.assertTrue(notifier.send("opt-in"))

        constructor.assert_called_once_with(
            "dummy-token-final-fix", parse_mode="HTML"
        )
        self.assertEqual(bot.messages, [("opt-in-chat", "opt-in")])

    def test_send_event_uses_photo_and_escapes_all_dynamic_content(self):
        """若事件渲染漏掉转义或没有走 sendPhoto，本测试必须失败。"""
        event_type = getattr(notifier, "NotificationEvent", None)
        self.assertIsNotNone(event_type, "NotificationEvent 尚未实现")
        send_event = getattr(notifier, "send_event", None)
        self.assertIsNotNone(send_event, "send_event 尚未实现")
        bot = FakeBot()
        self._install_bot(bot)

        event = event_type(
            title="新片 <入库>",
            fields=(("来源&类型", "光鸭 <云盘>"),),
            lines=("备注：A&B",),
            image_url="https://image.example/poster.jpg",
            footer="完成 > 等待刷新",
        )

        self.assertTrue(send_event(event))
        self.assertEqual(bot.messages, [])
        self.assertEqual(len(bot.photos), 1)
        target, photo, caption = bot.photos[0]
        self.assertEqual(target, "100")
        self.assertEqual(photo, "https://image.example/poster.jpg")
        self.assertIn("<b>🎬 新片 &lt;入库&gt;</b>", caption)
        self.assertIn("<b>☁️ 来源&amp;类型：</b>光鸭 &lt;云盘&gt;", caption)
        self.assertIn("备注：A&amp;B", caption)
        self.assertIn("完成 &gt; 等待刷新", caption)
        self.assertNotIn("<入库>", caption)
        self.assertNotIn("<云盘>", caption)

    def test_send_event_falls_back_to_text_when_photo_send_fails(self):
        """若图片异常导致通知丢失而非回退文本，本测试必须失败。"""
        class TelegramPhotoRejected(RuntimeError):
            result_json = {
                "error_code": 400,
                "description": "Bad Request: failed to get HTTP URL content",
            }

        event_type = getattr(notifier, "NotificationEvent", None)
        self.assertIsNotNone(event_type, "NotificationEvent 尚未实现")
        send_event = getattr(notifier, "send_event", None)
        self.assertIsNotNone(send_event, "send_event 尚未实现")
        bot = FakeBot(photo_error=TelegramPhotoRejected("photo unavailable"))
        self._install_bot(bot, "200")
        event = event_type(
            title="整理完成",
            fields=(("文件", "1 个"),),
            image_url="https://image.example/broken.jpg",
        )

        self.assertTrue(send_event(event))
        self.assertEqual(bot.photos, [])
        self.assertEqual(bot.messages, [("200", "<b>✅ 整理完成</b>\n<b>📄 文件：</b>1 个")])

    def test_unknown_photo_delivery_does_not_fallback_to_duplicate_text(self):
        bot = FakeBot(photo_error=TimeoutError("delivery outcome unknown"))
        self._install_bot(bot, "201")

        result = notifier.send_result(
            "<b>整理完成</b>", image_url="https://image.example/poster.jpg",
        )

        self.assertFalse(result.ok)
        self.assertEqual(bot.messages, [])

    def test_photo_success_does_not_retry_full_text_when_continuation_fails(self):
        """若图片已成功后续发失败又重发全文，可能造成首段重复。"""
        bot = FakeBot(message_error=RuntimeError("continuation unavailable"))
        self._install_bot(bot, "250")
        event = notifier.NotificationEvent(
            title="超长图片通知",
            lines=("A" * 1600,),
            image_url="https://image.example/poster.jpg",
        )

        self.assertFalse(notifier.send_event(event))
        self.assertEqual(len(bot.photos), 1)
        self.assertEqual(bot.message_attempts, 1)

    def test_partial_text_delivery_is_marked_unsafe_to_retry(self):
        class FailSecondMessageBot(FakeBot):
            def send_message(self, chat_id: str, text: str, **_kwargs) -> None:
                self.message_attempts += 1
                if self.message_attempts == 2:
                    error = RuntimeError("rate limited after first chunk")
                    error.result_json = {"error_code": 429, "description": "retry later"}
                    raise error
                self.messages.append((chat_id, text))

        bot = FailSecondMessageBot()
        self._install_bot(bot, "251")
        result = notifier.send_event_result(notifier.NotificationEvent(
            title="超长文本通知",
            lines=("A" * 5000,),
        ))

        self.assertFalse(result.ok)
        self.assertTrue(result.partially_delivered)
        self.assertTrue(result.outcome_unknown)
        self.assertEqual(result.status_code, 429)
        self.assertEqual(len(bot.messages), 1)

    def test_legacy_send_splits_at_natural_line_boundaries(self):
        """若长消息再次按固定字符切断自然行，本测试必须失败。"""
        bot = FakeBot()
        self._install_bot(bot, "300")
        first = "A" * 2500
        second = "B" * 2500

        self.assertTrue(notifier.send(f"{first}\n{second}"))

        self.assertEqual(bot.messages, [("300", first), ("300", second)])
        self.assertTrue(all(len(text) <= 4000 for _, text in bot.messages))

    def test_long_escaped_event_never_splits_inside_html_entity_or_bold_tag(self):
        """若分段切断 HTML entity 或粗体标签，本测试必须失败。"""
        event_type = getattr(notifier, "NotificationEvent", None)
        self.assertIsNotNone(event_type, "NotificationEvent 尚未实现")
        send_event = getattr(notifier, "send_event", None)
        self.assertIsNotNone(send_event, "send_event 尚未实现")
        bot = FakeBot()
        self._install_bot(bot, "400")
        event = event_type(title="长通知", lines=(("<&>" * 1400),))

        self.assertTrue(send_event(event))

        self.assertGreater(len(bot.messages), 1)
        for _, chunk in bot.messages:
            self.assertLessEqual(len(chunk), 4000)
            self.assertNotRegex(chunk, r"&(?:l|lt|g|gt|a|am|amp)?$")
            self.assertEqual(chunk.count("<b>"), chunk.count("</b>"))

    def test_oversized_html_tag_degrades_to_escaped_plain_text_chunks(self):
        """若超长标签仍作为单个 HTML token 输出，消息会超过 Telegram 上限。"""
        text = '<a href="' + ("x" * 5000) + '">链接</a>'

        chunks = notifier.split_message(text)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 4000 for chunk in chunks))
        rendered = "".join(chunks)
        self.assertIn("&lt;a href=&quot;", rendered)
        self.assertIn("&gt;链接&lt;/a&gt;", rendered)
        self.assertNotIn("<a href=", rendered)


class MediaCardTests(unittest.TestCase):
    def test_unidentified_media_items_return_no_cards_for_stats_fallback(self):
        """若缺少 TMDB 或标题仍生成成功卡片，统计退化路径将永远不会执行。"""
        self.assertEqual(notifier.build_media_events([{
            "title": "",
            "tmdb_id": "",
            "media_type": "movie",
            "filename": "unknown.mkv",
        }]), [])
        self.assertEqual(notifier.build_media_events([{
            "title": "只有标题",
            "tmdb_id": "",
            "media_type": "movie",
        }]), [])
        self.assertEqual(notifier.build_media_events([{
            "title": "",
            "tmdb_id": "123",
            "media_type": "tv",
        }]), [])

    def test_movie_card_contains_required_media_fields_and_tmdb_image(self):
        """若电影卡片遗漏规格字段、画质摘要或 TMDB 图片，本测试必须失败。"""
        build_media_events = getattr(notifier, "build_media_events", None)
        self.assertIsNotNone(build_media_events, "build_media_events 尚未实现")

        events = build_media_events([{
            "title": "流浪地球<2>",
            "year": "2023",
            "media_type": "movie",
            "tmdb_id": "533535",
            "source": "光鸭&云盘",
            "category": "电影/国产/科幻",
            "filename": "The.Wandering.Earth.II.2023.2160p.BluRay.DoVi.mkv",
            "size": 40 * 1024 ** 3,
            "backdrop_path": "/backdrop.jpg",
            "poster_path": "/poster.jpg",
        }])

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.image_url, "https://image.tmdb.org/t/p/w780/backdrop.jpg")
        rendered = notifier.render_event(event)
        self.assertIn("🎬 新片入库：流浪地球&lt;2&gt; (2023)", rendered)
        self.assertIn("<b>☁️ 来源：</b>光鸭&amp;云盘", rendered)
        self.assertIn("<b>🗂️ 分类：</b>电影 / 国产 / 科幻", rendered)
        self.assertIn("<b>🎛️ 版本：</b>2160p · BluRay · DoVi", rendered)
        self.assertIn("<b>📄 文件：</b>1 个", rendered)
        self.assertIn("<b>💾 体积：</b>40.00 GB", rendered)
        self.assertIn("<b>🎬 TMDB：</b>533535", rendered)

    def test_tv_card_groups_episode_range_by_tmdb_type_and_season(self):
        """若剧集未按季聚合或集数范围压缩错误，本测试必须失败。"""
        build_media_events = getattr(notifier, "build_media_events", None)
        self.assertIsNotNone(build_media_events, "build_media_events 尚未实现")
        common = {
            "title": "庆余年",
            "year": "2024",
            "media_type": "tv",
            "tmdb_id": "96462",
            "season": 2,
            "source": "光鸭云盘",
            "category": "剧集/国产/剧情",
            "season_total": 36,
            "version": "2160p WEB-DL HDR",
            "size": 2 * 1024 ** 3,
        }
        items = [
            {
                **common,
                "episode": episode,
                "season_present_episodes": [1, 2, 4],
            }
            for episode in (1, 2, 4)
        ]
        items.append({
            **common,
            "season": 3,
            "episode": 1,
            "season_total": 12,
            "size": 1024 ** 3,
            "season_present_episodes": list(range(1, 13)),
        })

        events = build_media_events(items)

        self.assertEqual(len(events), 2)
        season_two = notifier.render_event(events[0])
        self.assertIn("📺 剧集入库：庆余年 (2024) · S02", season_two)
        self.assertIn("<b>📺 本次：</b>E01-E02、E04", season_two)
        self.assertIn("<b>📊 本季：</b>3 / 36 集", season_two)
        self.assertIn("<b>🧩 缺集：</b>S02E03、S02E05-S02E36", season_two)
        self.assertIn("可能尚未下载、尚未播出或需人工确认", season_two)
        self.assertIn("<b>🎛️ 版本：</b>2160p · WEB-DL · HDR", season_two)
        self.assertIn("<b>📄 文件：</b>3 个", season_two)
        self.assertIn("<b>💾 体积：</b>6.00 GB", season_two)
        season_three = notifier.render_event(events[1])
        self.assertIn("· S03", season_three)
        self.assertIn("<b>📊 本季：</b>12 / 12 集（全）", season_three)
        self.assertNotIn("缺集", season_three)

    def test_partial_directory_does_not_claim_final_missing_episodes(self):
        items = [{
            "title": "平凡职业造就世界最强", "year": "2019",
            "media_type": "tv", "tmdb_id": "86034", "season": 1,
            "episode": 2, "season_total": 13,
            "season_present_episodes": [2, 6],
            "source": "光鸭云盘", "category": "动漫",
        }]

        rendered = notifier.render_event(
            notifier.build_media_events(items, inventory_final=False)[0]
        )

        self.assertIn("<b>📊 本季：</b>2 / 13 集", rendered)
        self.assertNotIn("<b>🧩 缺集：</b>", rendered)
        self.assertIn("暂不生成最终缺集结论", rendered)

    def test_skipped_tv_directory_does_not_claim_final_missing_episodes(self):
        from app.modules.organize import Organizer, OrganizeRules

        sent = []
        stats = {
            "directories": {
                "批次": {
                    "total": 2, "moved": 1, "metadata_moved": 0,
                    "skipped": 1, "need_confirm": 0, "failed": 0,
                },
            },
            "media_items": [{
                "directory": "批次", "title": "测试剧", "year": "2026",
                "media_type": "tv", "tmdb_id": "100", "season": 1,
                "episode": 1, "season_total": 3,
                "season_present_episodes": [1], "source": "光鸭云盘",
                "category": "动漫",
            }],
        }

        with patch("app.notifier.send_event", side_effect=lambda event, chat_id=None: sent.append(event) or True):
            Organizer.notify_directory_results(stats, OrganizeRules(), source_name="下载")

        media = next(event for event in sent if "剧集入库" in str(event.title))
        rendered = notifier.render_event(media)
        self.assertNotIn("<b>🧩 缺集：</b>", rendered)
        self.assertIn("暂不生成最终缺集结论", rendered)

    def test_scan_incomplete_task_does_not_claim_final_missing_episodes(self):
        from app.modules.organize import Organizer, OrganizeRules

        sent_text = []
        stats = {
            "total": 1, "moved": 1, "metadata_moved": 0,
            "need_confirm": 0, "skipped": 0, "failed": 0,
            "scan_complete": False,
            "scan_errors": ["source: 目录读取失败"],
            "media_items": [{
                "title": "测试剧", "year": "2026", "media_type": "tv",
                "tmdb_id": "100", "season": 1, "episode": 1,
                "season_total": 3, "season_present_episodes": [1],
                "source": "光鸭云盘", "category": "动漫",
            }],
        }

        with patch(
            "app.modules.organize_notification_outbox.deliver_organize_notification",
            side_effect=lambda key, text, chat_id="": sent_text.append(text) or True,
        ):
            Organizer.notify_task_results(stats, OrganizeRules(), source_name="1 个源目录")

        rendered = "\n".join(sent_text)
        self.assertIn("光鸭整理部分完成", rendered)
        self.assertNotIn("<b>🧩 缺集：</b>", rendered)
        self.assertIn("目录扫描未完整结束", rendered)

    def test_media_card_without_identification_data_falls_back_to_stats(self):
        """若业务数据不足时卡片为空或报错，而非结构化统计，本测试必须失败。"""
        build_stats_event = getattr(notifier, "build_stats_event", None)
        self.assertIsNotNone(build_stats_event, "build_stats_event 尚未实现")

        event = build_stats_event(
            "光鸭整理完成 <A>",
            {
                "总视频": 5,
                "已移动": 3,
                "失败": 1,
                "空值": "",
            },
            footer="目录：来源&一",
        )
        rendered = notifier.render_event(event)

        self.assertIn("光鸭整理完成 &lt;A&gt;", rendered)
        self.assertIn("<b>🎞️ 总视频：</b>5", rendered)
        self.assertIn("<b>📥 已移动：</b>3", rendered)
        self.assertIn("<b>❌ 失败：</b>1", rendered)
        self.assertNotIn("空值", rendered)
        self.assertIn("目录：来源&amp;一", rendered)


class BusinessNotificationIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        notifier.reset()
        self.bot = FakeBot()
        notifier._bot = self.bot
        notifier._chat_id = "test-only"
        notifier._initialized = True

    def tearDown(self) -> None:
        notifier.reset()

    def test_organize_directory_prefers_media_cards_and_falls_back_to_stats(self):
        """若整理结果仍直接拼 HTML 或识别数据不足时不回退，本测试必须失败。"""
        from unittest.mock import patch
        from app.modules.organize import Organizer, OrganizeRules

        sent = []
        stats = {
            "directories": {
                "电影目录": {"total": 1, "moved": 1, "metadata_moved": 0, "skipped": 0, "failed": 0},
                "未知目录<&>": {"total": 2, "moved": 0, "metadata_moved": 0, "skipped": 1, "failed": 1},
            },
            "media_items": [{
                "directory": "电影目录",
                "title": "流浪地球2",
                "year": "2023",
                "media_type": "movie",
                "tmdb_id": "533535",
                "source": "光鸭云盘",
                "category": "电影/国产",
                "filename": "demo.2160p.BluRay.mkv",
                "size": 1024 ** 3,
            }],
        }
        self.assertTrue(hasattr(notifier, "send_event"))
        with patch("app.notifier.send_event", side_effect=lambda event, chat_id=None: sent.append(event) or True):
            Organizer.notify_directory_results(stats, OrganizeRules(), source_name="来源<&>")

        self.assertEqual(len(sent), 2)
        rendered = [notifier.render_event(event) for event in sent]
        self.assertTrue(any("新片入库" in text for text in rendered))
        fallback = next(text for text in rendered if "整理目录部分完成" in text)
        self.assertIn("未知目录&lt;&amp;&gt;", fallback)
        self.assertNotIn("未知目录<&>", fallback)

    def test_unidentified_directory_media_uses_real_stats_fallback(self):
        """若无有效识别信息时没有真正发送统计卡片，本测试必须失败。"""
        from unittest.mock import patch
        from app.modules.organize import Organizer, OrganizeRules

        sent = []
        stats = {
            "directories": {
                "待确认": {
                    "total": 2, "moved": 0, "metadata_moved": 0,
                    "skipped": 1, "need_confirm": 1, "failed": 0,
                },
            },
            "media_items": [{
                "directory": "待确认",
                "title": "",
                "tmdb_id": "",
                "media_type": "movie",
                "filename": "unknown.mkv",
                "size": 123,
            }],
        }
        with patch("app.notifier.send_event", side_effect=lambda event, chat_id=None: sent.append(event) or True):
            Organizer.notify_directory_results(stats, OrganizeRules(), source_name="下载")

        self.assertEqual(len(sent), 1)
        rendered = notifier.render_event(sent[0])
        self.assertIn("整理目录部分完成", rendered)
        self.assertIn(
            "<b>处理结果</b>  视频 2 个 · 需要确认 1 个 · 跳过 1 个",
            rendered,
        )
        self.assertNotIn("新片入库", rendered)

    def test_partial_directory_sends_media_card_and_warning_stats(self):
        """若部分成功目录只发送成功卡片，失败和待确认会被隐藏。"""
        from unittest.mock import patch
        from app.modules.organize import Organizer, OrganizeRules

        sent = []
        stats = {
            "directories": {
                "混合目录": {
                    "total": 4, "moved": 1, "metadata_moved": 0,
                    "skipped": 2, "need_confirm": 1, "failed": 1,
                    "skip_reasons": ["目标已有同版本", "同批次较小文件已淘汰"],
                },
            },
            "media_items": [{
                "directory": "混合目录",
                "title": "流浪地球2",
                "year": "2023",
                "media_type": "movie",
                "tmdb_id": "533535",
                "source": "光鸭云盘",
                "category": "电影/国产",
                "filename": "demo.2160p.mkv",
                "size": 1024 ** 3,
            }],
        }
        with patch("app.notifier.send_event", side_effect=lambda event, chat_id=None: sent.append(event) or True):
            Organizer.notify_directory_results(stats, OrganizeRules(), source_name="下载")

        self.assertEqual(len(sent), 2)
        media = next(event for event in sent if "入库：" in str(event.title))
        warning = next(event for event in sent if "部分完成" in str(event.title))
        self.assertFalse(media.footer)
        rendered = notifier.render_event(warning)
        self.assertIn(
            "<b>处理结果</b>  视频 4 个 · 成功入库 1 个 · "
            "需要确认 1 个 · 跳过 2 个 · 失败 1 个",
            rendered,
        )
        self.assertIn("跳过原因：目标已有同版本；同批次较小文件已淘汰", rendered)


    def test_task_summary_uses_compact_sections_instead_of_inline_candidate_dump(self):
        from app.modules.organize import Organizer

        counts = {
            "视频": 302,
            "已移动": 187,
            "需确认": 2,
            "跳过": 113,
        }
        footer = Organizer._task_notification_footer(
            {
                "need_confirm": 2,
                "skipped": 113,
                "skip_reasons": [
                    "同版本仍按冲突策略处理：保留现有版本并跳过新文件",
                    "批内同版本冲突：由更优版本胜出，未执行云盘写入",
                ],
            },
            confirmation_group_count=2,
        )

        self.assertEqual(
            Organizer._notification_count_summary(counts, compact=True),
            "视频 302 · 入库 187 · 待确认 2 · 跳过 113",
        )
        self.assertIn("⚠️ 待确认 2 组", footer)
        self.assertIn("请在下方候选卡中选择匹配结果", footer)
        self.assertIn("⏭️ 跳过 113 个", footer)
        self.assertIn("• 同版本仍按冲突策略处理", footer)
        self.assertNotIn("最佳候选", footer)

    def test_bad_numeric_media_data_does_not_block_later_directory_notifications(self):
        """若单条坏数值中断目录循环，后续目录通知会全部丢失。"""
        from unittest.mock import patch
        from app.modules.organize import Organizer, OrganizeRules

        sent = []
        stats = {
            "directories": {
                "坏数据目录": {"total": 1, "moved": 1, "metadata_moved": 0, "skipped": 0, "need_confirm": 0, "failed": 0},
                "正常目录": {"total": 1, "moved": 1, "metadata_moved": 0, "skipped": 0, "need_confirm": 0, "failed": 0},
            },
            "media_items": [
                {
                    "directory": "坏数据目录", "title": "坏数据剧", "year": "2026",
                    "media_type": "tv", "tmdb_id": "bad-1", "season": 2,
                    "episode": 1, "season_total": "未知", "size": "无法转换",
                    "source": "光鸭云盘", "filename": "Bad.S02E01.mkv",
                },
                {
                    "directory": "正常目录", "title": "正常电影", "year": "2025",
                    "media_type": "movie", "tmdb_id": "good-1", "size": 1024,
                    "source": "光鸭云盘", "filename": "Good.1080p.mkv",
                },
            ],
        }
        with patch("app.notifier.send_event", side_effect=lambda event, chat_id=None: sent.append(event) or True):
            Organizer.notify_directory_results(stats, OrganizeRules(), source_name="下载")

        self.assertEqual(len(sent), 2)
        rendered = [notifier.render_event(event) for event in sent]
        self.assertTrue(any(
            "坏数据剧" in text
            and "<b>本次</b>  E01" in text
            and "<b>本季</b>" not in text
            and "<b>文件</b>  1 个" in text
            for text in rendered
        ))
        self.assertTrue(any("正常电影" in text for text in rendered))

    def test_download_and_scheduler_notifications_use_structured_events(self):
        """若下载或 STRM 通知绕过统一事件模型，本测试必须失败。"""
        from unittest.mock import patch
        from app.modules import download_tracker, scheduler

        self.assertTrue(hasattr(download_tracker, "send_event"), "下载通知尚未接入 send_event")
        self.assertTrue(hasattr(scheduler, "send_event"), "STRM 通知尚未接入 send_event")
        sent = []
        row = {"status": "downloading", "title": "任务<&>", "chat_id": "900"}
        with patch.object(download_tracker, "send", side_effect=lambda event, chat_id=None: sent.append((event, chat_id)) or True):
            download_tracker.DownloadTracker._notify_completion(
                row, "completed", "failed", {"status": "completed"}
            )
            download_tracker.DownloadTracker._notify_completion(
                row, "manual_review", "completed", {"status": "manual_review"}
            )
        with patch.object(scheduler, "get_bool", return_value=True), patch.object(
            scheduler, "send", side_effect=lambda event, chat_id=None: sent.append((event, chat_id)) or True
        ):
            scheduler.STRMScheduler._notify_failure("错误<&>", "cron")

        self.assertEqual(len(sent), 3)
        download_text = notifier.render_event(sent[0][0])
        review_text = notifier.render_event(sent[1][0])
        failure_text = notifier.render_event(sent[2][0])
        self.assertIn("任务&lt;&amp;&gt;", download_text)
        self.assertNotIn("任务<&>", download_text)
        self.assertEqual(sent[0][1], "900")
        self.assertIn("需要人工核对", review_text)
        self.assertIn("请勿重复提交", review_text)
        self.assertEqual(sent[1][1], "900")
        self.assertIn("错误&lt;&amp;&gt;", failure_text)
        self.assertNotIn("错误<&>", failure_text)

class OrganizeMediaDataTests(unittest.TestCase):
    def test_plan_one_tolerates_invalid_tmdb_and_parsed_season_numbers(self):
        """异常季号或集数不得从 _plan_one 冒泡并中断整个整理扫描。"""
        from unittest.mock import Mock
        from app.clients.guangya import GuangYaFile
        from app.modules.organize import Organizer, OrganizeRules
        from app.modules.scraper import MatchResult

        cases = (
            (
                "parsed-season",
                {"season": "未知季", "episode": 1},
                [{"season_number": 2, "episode_count": 12}],
                0,
            ),
            (
                "tmdb-season-number",
                {"season": "2", "episode": 1},
                [
                    {"season_number": "未知季", "episode_count": 99},
                    {"season_number": 2, "episode_count": 12},
                ],
                12,
            ),
            (
                "tmdb-episode-count",
                {"season": 2, "episode": 1},
                [{"season_number": 2, "episode_count": "未知集数"}],
                0,
            ),
        )
        for label, parsed, seasons, expected_total in cases:
            with self.subTest(label=label):
                scraper = Mock()
                scraper.match.return_value = MatchResult(
                    tmdb_id="96462", title="庆余年", year="2024", media_type="tv"
                )
                scraper.parse_media = Mock(
                    side_effect=lambda filename, parent_path="", match=None, parsed=parsed: release_parse_result(
                        dict(parsed), filename=filename, parent_path=parent_path,
                    )
                )
                scraper.get_detail.return_value = {
                    "origin_country": ["CN"],
                    "genres": [],
                    "seasons": seasons,
                }
                organizer = Organizer(client=object(), scraper=scraper)
                file = GuangYaFile(
                    f"file-{label}", "Show.S02E01.mkv", False,
                    1024, "etag", "source",
                )

                try:
                    plan = organizer._plan_one(file, "下载目录", OrganizeRules())
                except (TypeError, ValueError, OverflowError) as exc:
                    self.fail(f"_plan_one 不应因异常数字中断: {exc}")

                self.assertEqual(plan.action, "move")
                self.assertEqual(plan.season_total, expected_total)
                self.assertTrue(plan.new_name)

    def test_successful_move_exposes_media_data_for_rich_card(self):
        """若整理成功后未产生可供富媒体通知聚合的数据，本测试必须失败。"""
        from unittest.mock import Mock, patch
        from app.modules.organize import Organizer, OrganizePlan, OrganizeRules
        from app.modules.organize_execution import execute_organize_plans
        from app.modules.scraper import MatchResult

        client = Mock()
        client.list_dir.return_value = []
        organizer = Organizer(client=client, scraper=object())
        organizer._ensure_dir_chain = Mock(return_value="target")
        organizer._find_file = Mock(return_value=None)
        plan = OrganizePlan(
            file_id="file-1",
            original_name="Show.S02E04.2160p.WEB-DL.HDR.mkv",
            original_path="下载目录",
            original_parent_id="source",
            size=2 * 1024 ** 3,
            match=MatchResult(
                tmdb_id="96462", title="庆余年", year="2024", media_type="tv"
            ),
            main_category="剧集",
            region="国产",
            year="2024",
            new_name="Show.S02E04.2160p.WEB-DL.HDR.mkv",
            target_path="剧集/国产/2024/庆余年",
            backdrop_path="/show.jpg",
            season=2,
            episode=4,
            season_total=36,
            action="move",
        )
        stats = {"moved": 0, "metadata_moved": 0, "renamed": 0, "failed": 0}

        with patch("app.modules.organize.add_organize_log", return_value=1), patch(
            "app.modules.organize.add_organize_log_items"
        ):
            execute_organize_plans(organizer, [plan], OrganizeRules(rename_enabled=False), stats, {})

        self.assertEqual(len(stats.get("media_items") or []), 1)
        item = stats["media_items"][0]
        self.assertEqual(item["title"], "庆余年")
        self.assertEqual(item["season"], 2)
        self.assertEqual(item["episode"], 4)
        self.assertEqual(item["season_total"], 36)
        self.assertEqual(item["source"], "光鸭云盘")
        self.assertEqual(item["category"], "剧集/国产/2024/庆余年")
        self.assertEqual(item["backdrop_path"], "/show.jpg")


if __name__ == "__main__":
    unittest.main()
