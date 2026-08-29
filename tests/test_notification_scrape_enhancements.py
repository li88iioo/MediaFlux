"""Telegram、STRM 与刮削预览增强回归测试。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tests.support import release_parse_result

from app.clients.tmdb import TMDBClient
from app.modules.scraper import Candidate, MatchResult, TMDBScraper
from app.clients.guangya import GuangYaClient, GuangYaFile
from app.modules.organize import Organizer, OrganizeRules
from app.modules.scheduler import STRMScheduler
from app.modules.strm import sync_strm
from app import notifier
from app.notifier import NotificationEvent
from tests.support import IsolatedDatabaseTestCase


class _RaisingTMDBClient:
    api_key = "key"
    base_url = "https://api.themoviedb.org/3"
    session = None
    config_error = ""

    def search(self, title, year, media_type):
        raise RuntimeError("连接超时")

    def detail(self, tmdb_id, media_type):
        return {}


class _EmptyTMDBClient:
    api_key = "key"
    base_url = "https://api.themoviedb.org/3"
    session = None
    config_error = ""

    def search(self, title, year, media_type):
        return []

    def detail(self, tmdb_id, media_type):
        return {}


class TMDBDiagnosticTests(unittest.TestCase):
    def test_empty_tmdb_url_uses_official_default(self):
        client = TMDBClient(config={"TMDB_API_KEY": "key", "TMDB_API_URL": ""})
        self.assertEqual(client.base_url, "https://api.themoviedb.org/3")
        self.assertEqual(client.config_error, "")

    def test_candidate_detail_requests_alternative_titles(self):
        response = Mock(status_code=200)
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "id": 86034,
            "alternative_titles": {
                "results": [{"title": "Arifureta Shokugyou de Sekai Saikyou"}]
            },
        }
        session = Mock()
        session.get.return_value = response
        client = TMDBClient(api_key="key", session=session, retries=0)

        detail = client.detail_with_alternative_titles("86034", "tv")

        self.assertEqual(detail["id"], 86034)
        session.get.assert_called_once()
        url = session.get.call_args.args[0]
        kwargs = session.get.call_args.kwargs
        self.assertEqual(url, "https://api.themoviedb.org/3/tv/86034")
        self.assertEqual(
            kwargs["params"]["append_to_response"],
            "alternative_titles,translations",
        )
        self.assertEqual(kwargs["params"]["language"], "zh-CN")
        self.assertFalse(kwargs["allow_redirects"])

    def test_invalid_tmdb_url_becomes_config_diagnostic_without_http(self):
        session = Mock()
        client = TMDBClient(
            config={"TMDB_API_KEY": "key", "TMDB_API_URL": "tmdb.invalid/api"},
            session=session,
        )
        scraper = TMDBScraper(client=client)
        with patch.object(scraper, "_get_lock", return_value=None):
            result = scraper.match("Dilig.2024.1080p.WEB-DL.mkv")

        self.assertEqual(result.status, "config_error")
        self.assertTrue(result.need_confirm)
        self.assertIn("TMDB API URL", result.error)
        session.get.assert_not_called()

    def test_request_failure_is_not_reported_as_no_result(self):
        scraper = TMDBScraper(client=_RaisingTMDBClient())
        with patch.object(scraper, "_get_lock", return_value=None):
            result = scraper.match("Dilig.2024.1080p.WEB-DL.mkv")

        self.assertEqual(result.status, "request_error")
        self.assertTrue(result.need_confirm)
        self.assertIn("连接超时", result.error)

    def test_successful_empty_search_is_no_result(self):
        scraper = TMDBScraper(client=_EmptyTMDBClient())
        result = scraper.deterministic_recognize("Dilig.2024.1080p.WEB-DL.mkv")

        self.assertEqual(result.status, "no_result")
        self.assertTrue(result.need_confirm)
        self.assertEqual(result.error, "TMDB 无搜索结果")


class OrganizeConfirmationReasonTests(unittest.TestCase):
    def test_need_confirm_uses_match_error_in_plan(self):
        scraper = Mock()
        scraper.match.return_value = MatchResult(
            media_type="movie",
            need_confirm=True,
            status="request_error",
            error="TMDB 请求失败：连接超时",
        )
        organizer = Organizer(client=Mock(), scraper=scraper)

        plan = organizer._plan_one(
            GuangYaFile("1", "Dilig.2024.mkv", False),
            "待整理",
            OrganizeRules(),
        )

        self.assertEqual(plan.action, "skip")
        self.assertEqual(plan.note, "TMDB 请求失败：连接超时")

    def test_low_confidence_summary_contains_best_candidate(self):
        match = MatchResult(
            tmdb_id="1510055",
            title="狂怒者：荣誉之战",
            year="2026",
            media_type="movie",
            confidence=0.86,
            need_confirm=True,
            status="low_confidence",
            error="匹配置信度 86% 低于严格模式阈值 90%",
            candidates=[Candidate("1510055", "狂怒者：荣誉之战", "2026", 0.86, "movie")],
        )

        summary = Organizer._confirmation_summary(match)

        self.assertIn("狂怒者：荣誉之战", summary)
        self.assertIn("86%", summary)
        self.assertIn("TMDB 1510055", summary)


class NotificationEmojiTests(unittest.TestCase):
    def test_plain_event_gets_semantic_title_and_field_emojis(self):
        rendered = notifier.render_event(NotificationEvent(
            "光鸭整理完成",
            fields=(("目录", "来源一"), ("失败", 0), ("耗时", "1.2 秒")),
        ))

        self.assertIn("<b>✅ 光鸭整理完成</b>", rendered)
        self.assertIn("<b>📂 目录：</b>来源一", rendered)
        self.assertIn("<b>❌ 失败：</b>0", rendered)
        self.assertIn("<b>⏱️ 耗时：</b>1.2 秒", rendered)

    def test_relaxed_layout_adds_whitespace_without_field_emoji_wall(self):
        rendered = notifier.render_event(NotificationEvent(
            "⚠️ 发现需要确认的媒体",
            fields=(("媒体", "不要欺负我，长瀞同学"), ("剧集", "第 2 季 · E01–E12")),
            footer="匹配结果需要人工确认\n\n请选择下方候选继续整理。",
            layout="relaxed",
        ))

        self.assertIn("</b>\n\n<b>媒体</b>  不要欺负我，长瀞同学", rendered)
        self.assertIn("<b>剧集</b>  第 2 季 · E01–E12", rendered)
        self.assertNotIn("📺 剧集", rendered)
        self.assertIn("E01–E12\n\n匹配结果需要人工确认", rendered)

    def test_field_emoji_opt_out_preserves_compact_default_layout(self):
        rendered = notifier.render_event(NotificationEvent(
            "STRM 同步结果",
            fields=(("目录", "来源一"), ("状态", "✅ 同步完成")),
            field_emojis=False,
        ))

        self.assertIn("<b>🧩 STRM 同步结果</b>", rendered)
        self.assertIn("<b>目录：</b>来源一", rendered)
        self.assertIn("<b>状态：</b>✅ 同步完成", rendered)
        self.assertNotIn("📂 目录", rendered)

    def test_existing_title_and_field_emojis_are_not_duplicated(self):
        rendered = notifier.render_event(NotificationEvent(
            "✅ STRM 同步完成",
            fields=(("📂 目录", "A"),),
        ))

        self.assertIn("<b>✅ STRM 同步完成</b>", rendered)
        self.assertIn("<b>📂 目录：</b>A", rendered)
        self.assertNotIn("✅ ✅", rendered)
        self.assertNotIn("📂 📂", rendered)

    def test_error_title_uses_failure_emoji_before_service_emoji(self):
        rendered = notifier.render_event(NotificationEvent("STRM 同步失败"))
        self.assertIn("<b>❌ STRM 同步失败</b>", rendered)

    def test_organize_command_uses_temporary_progress_without_redundant_start_event(self):
        from app.bot import handlers

        manager = Mock()
        manager.start.return_value = {"ok": True, "task_id": "task-1", "run_id": 41}
        manager.task_result.return_value = {
            "id": "task-1", "status": "completed", "current_source": "",
            "notification_sent": True,
        }
        progress = Mock()
        handlers._task_lock.acquire()
        with patch("app.modules.organize_tasks.get_organize_manager", return_value=manager), patch.object(
            handlers, "send"
        ) as send_mock:
            handlers._do_organize(
                "123", [{"id": "source", "name": "电影"}], "target", progress
            )

        send_mock.assert_not_called()
        progress.bind_task_run.assert_called_once_with("guangya_organize", 41)
        progress.dismiss_source_message.assert_called_once_with()
        manager.task_result.assert_called_once_with("task-1")
        manager.task_status.assert_not_called()
        progress.dismiss.assert_called_once_with("光鸭整理已结束。")
        progress.finish.assert_not_called()

    def test_organize_command_keeps_terminal_result_when_summary_was_not_sent(self):
        from app.bot import handlers

        manager = Mock()
        manager.start.return_value = {"ok": True, "task_id": "task-2", "run_id": 42}
        manager.task_result.return_value = {
            "id": "task-2",
            "status": "partial",
            "current_source": "",
            "notification_sent": False,
            "stats": {"total": 3, "moved": 2, "need_confirm": 1, "failed": 0},
        }
        progress = Mock()
        handlers._task_lock.acquire()
        with patch("app.modules.organize_tasks.get_organize_manager", return_value=manager):
            handlers._do_organize(
                "123", [{"id": "source", "name": "电影"}], "target", progress
            )

        progress.bind_task_run.assert_called_once_with("guangya_organize", 42)
        progress.dismiss_source_message.assert_called_once_with()
        manager.task_result.assert_called_once_with("task-2")
        manager.task_status.assert_not_called()
        progress.dismiss.assert_not_called()
        progress.finish.assert_called_once()
        rendered = progress.finish.call_args.args[0]
        self.assertIn("光鸭整理部分完成", rendered)
        self.assertIn("视频 3", rendered)
        self.assertIn("需确认 1", rendered)


    def test_organize_command_fails_closed_when_task_identity_is_missing(self):
        from app.bot import handlers

        manager = Mock()
        manager.start.return_value = {"ok": True, "task_id": "task-missing", "run_id": 43}
        manager.task_result.return_value = None
        progress = Mock()
        handlers._task_lock.acquire()
        with patch("app.modules.organize_tasks.get_organize_manager", return_value=manager):
            handlers._do_organize(
                "123", [{"id": "source", "name": "电影"}], "target", progress
            )

        progress.bind_task_run.assert_called_once_with("guangya_organize", 43)
        manager.task_result.assert_called_once_with("task-missing")
        manager.task_status.assert_not_called()
        progress.dismiss.assert_not_called()
        progress.finish.assert_called_once()
        rendered = progress.finish.call_args.args[0]
        self.assertIn("光鸭整理失败", rendered)
        self.assertIn("状态不可恢复", rendered)

    def test_organize_summary_reports_delivery_failure(self):
        stats = {
            "total": 1, "moved": 1, "metadata_moved": 0,
            "need_confirm": 0, "skipped": 0, "failed": 0,
            "media_items": [], "confirmation_groups": [],
        }
        with patch(
            "app.modules.telegram_organize_lifecycle.publish_organize_lifecycle",
            return_value=False,
        ):
            delivered = Organizer.notify_task_results(
                stats, OrganizeRules(), source_name="1 个源目录", chat_id="123"
            )
        self.assertFalse(delivered)

    def test_organize_summary_disabled_reports_not_delivered(self):
        self.assertFalse(Organizer.notify_task_results(
            {"total": 0}, OrganizeRules(notify_enabled=False), chat_id="123"
        ))

    def test_organize_command_keeps_confirmation_when_task_is_rejected(self):
        from app.bot import handlers

        manager = Mock()
        manager.start.return_value = {"ok": False, "error": "目录不可用"}
        progress = Mock()
        handlers._task_lock.acquire()
        with patch(
            "app.modules.organize_tasks.get_organize_manager",
            return_value=manager,
        ):
            handlers._do_organize(
                "123", [{"id": "source", "name": "电影"}], "target", progress
            )

        progress.dismiss_source_message.assert_not_called()
        progress.finish.assert_called_once()



class TelegramStrmCleanupNotificationTests(unittest.TestCase):
    def test_source_result_marks_incremental_fallback_as_partial(self):
        from app.bot import handlers

        event = handlers._strm_source_result_event({
            "id": "source", "name": "来源", "local_dir": "/tmp/strm",
            "stats": {"fallback_required": True},
        })

        self.assertEqual(dict(event.fields)["状态"], "⚠️ 部分完成")

    def test_sync_command_sends_one_compact_report_with_source_overview(self):
        from app.bot import handlers

        source_stats = {
            "total": 8, "generated": 3, "created": 2, "updated": 1,
            "skipped": 5, "failed": 0, "directories": 12, "scanned_files": 80,
            "metadata_generated": 4, "metadata_queued": 3, "metadata_cleaned": 1,
            "cleaned": 1, "empty_dirs_cleaned": 2, "clean_skipped": False,
            "scan_elapsed_seconds": 1.25, "generate_elapsed_seconds": 0.5,
            "metadata_elapsed_seconds": 0.25, "cleanup_elapsed_seconds": 0.1,
            "changes": [],
        }
        aggregate = {
            **source_stats,
            "changes": [{
                "action": "removed",
                "directory": "电影/示例",
                "filename": "<旧文件>.strm",
                "error": "",
            }, {
                "action": "removed_dir",
                "directory": "电影",
                "filename": "示例",
                "error": "",
            }],
        }
        scheduler = Mock()
        scheduler.run_blocking.return_value = {
            "ok": True,
            "stats": aggregate,
            "sources": [{
                "id": "source-1", "name": "整理<&>",
                "local_dir": "/app/strm/光鸭云盘/整理", "stats": source_stats,
            }],
            "media_refresh": {"Jellyfin": True},
            "elapsed_seconds": 2.4,
        }
        self.assertTrue(handlers._task_lock.acquire(timeout=1))
        try:
            with patch("app.modules.scheduler.get_scheduler", return_value=scheduler), patch.object(
                handlers, "get_bot", return_value=Mock()
            ), patch.object(handlers, "send") as send_mock:
                handlers._do_sync("123")
        finally:
            if handlers._task_lock.locked():
                handlers._task_lock.release()

        self.assertEqual(scheduler.run_blocking.call_args.args[0], "telegram")
        self.assertEqual(scheduler.run_blocking.call_args.kwargs["sync_mode"], "full")
        send_mock.assert_called_once()
        event = send_mock.call_args.args[0]
        self.assertIsInstance(event, NotificationEvent)
        self.assertEqual(event.title, "光鸭 STRM 同步全部完成")
        self.assertEqual(event.layout, "relaxed")
        fields = dict(event.fields)
        self.assertEqual(fields["状态"], "✅ 同步完成")
        self.assertEqual(fields["概览"], "1 个来源 · 2.40 秒")
        self.assertEqual(fields["扫描"], "12 个目录 · 80 个文件")
        self.assertEqual(fields["STRM"], "2 新建 · 1 更新 · 5 跳过 · 0 失败")
        self.assertEqual(fields["元数据"], "4 更新 · 3 后台排队")
        self.assertEqual(fields["清理"], "1 个无效 STRM · 1 个失效元数据 · 2 个空目录")
        self.assertEqual(fields["媒体库"], "Jellyfin 成功")
        self.assertEqual(event.lines[0], "来源概览")
        self.assertIn("• 整理<&>：80 文件 · 2 新建 · 1 更新 · 5 跳过", event.lines[1])
        self.assertIn("3 元数据排队 · 2.10 秒", event.lines[1])
        self.assertEqual(event.footer, "本轮 2 条清理明细已记录到 Web 运行记录。")
        rendered = notifier.render_event(event)
        self.assertIn("<b>✅ 光鸭 STRM 同步全部完成</b>", rendered)
        self.assertIn("<b>媒体库</b>  Jellyfin 成功", rendered)
        self.assertIn("整理&lt;&amp;&gt;", rendered)
        self.assertLess(len(rendered), 4000)

    def test_sync_command_folds_extra_sources_inside_the_same_report(self):
        from app.bot import handlers

        source_stats = {
            "directories": 1, "scanned_files": 1, "created": 1,
            "updated": 0, "skipped": 0, "failed": 0,
        }
        scheduler = Mock()
        scheduler.run_blocking.return_value = {
            "ok": True,
            "stats": {**source_stats, "changes": []},
            "sources": [
                {
                    "id": f"source-{index}", "name": f"来源 {index}",
                    "local_dir": f"/tmp/strm/{index}", "stats": source_stats,
                }
                for index in range(14)
            ],
            "media_refresh": {},
            "elapsed_seconds": 1.0,
        }
        self.assertTrue(handlers._task_lock.acquire(timeout=1))
        try:
            with patch("app.modules.scheduler.get_scheduler", return_value=scheduler), patch.object(
                handlers, "get_bot", return_value=Mock()
            ), patch.object(handlers, "send") as send_mock:
                handlers._do_sync("123")
        finally:
            if handlers._task_lock.locked():
                handlers._task_lock.release()

        send_mock.assert_called_once()
        event = send_mock.call_args.args[0]
        self.assertEqual(dict(event.fields)["概览"], "14 个来源 · 1.00 秒")
        source_lines = [line for line in event.lines if line.startswith("• ")]
        self.assertEqual(len(source_lines), 12)
        self.assertIn("另有 2 个来源已折叠", event.lines[-1])
        self.assertLess(len(notifier.render_event(event)), 4000)

    def test_sync_command_keeps_cleanup_details_in_web_record_only(self):
        from app.bot import handlers

        changes = [
            {
                "action": "removed",
                "directory": "动漫/示例/Season 01",
                "filename": f"E{index:03d}.strm",
                "error": "",
            }
            for index in range(81)
        ]
        scheduler = Mock()
        scheduler.run_blocking.return_value = {
            "ok": True,
            "stats": {
                "changes": changes, "cleaned": len(changes),
                "metadata_cleaned": 0, "empty_dirs_cleaned": 0,
            },
            "sources": [],
            "media_refresh": {},
            "elapsed_seconds": 1.0,
        }
        self.assertTrue(handlers._task_lock.acquire(timeout=1))
        try:
            with patch("app.modules.scheduler.get_scheduler", return_value=scheduler), patch.object(
                handlers, "get_bot", return_value=Mock()
            ), patch.object(handlers, "send") as send_mock:
                handlers._do_sync("123")
        finally:
            if handlers._task_lock.locked():
                handlers._task_lock.release()

        send_mock.assert_called_once()
        event = send_mock.call_args.args[0]
        self.assertEqual(event.footer, "本轮 81 条清理明细已记录到 Web 运行记录。")
        self.assertEqual(dict(event.fields)["清理"], "81 个无效 STRM · 0 个失效元数据 · 0 个空目录")

    def test_sync_command_finishes_progress_with_the_only_terminal_report(self):
        from app.bot import handlers

        scheduler = Mock()
        scheduler.run_blocking.return_value = {
            "ok": True,
            "stats": {"directories": 1, "scanned_files": 2, "changes": []},
            "sources": [],
            "media_refresh": {},
            "elapsed_seconds": 1.0,
        }
        progress = Mock()
        progress.source_message = None
        progress.finish.return_value = True
        self.assertTrue(handlers._task_lock.acquire(timeout=1))
        try:
            with patch("app.modules.scheduler.get_scheduler", return_value=scheduler), patch.object(
                handlers, "get_bot", return_value=Mock()
            ), patch.object(handlers, "send") as send_mock:
                handlers._do_sync("123", progress)
        finally:
            if handlers._task_lock.locked():
                handlers._task_lock.release()

        send_mock.assert_not_called()
        progress.finish.assert_called_once()
        self.assertIn("光鸭 STRM 同步全部完成", progress.finish.call_args.args[0])
        progress.dismiss.assert_not_called()

    def test_sync_command_explains_when_cleanup_is_safely_skipped(self):
        from app.bot import handlers

        scheduler = Mock()
        scheduler.run_blocking.return_value = {
            "ok": True,
            "stats": {
                "total": 1, "generated": 0, "skipped": 1, "failed": 0,
                "cleaned": 0, "metadata_cleaned": 0, "empty_dirs_cleaned": 0,
                "clean_skipped": True, "changes": [],
            },
        }
        self.assertTrue(handlers._task_lock.acquire(timeout=1))
        try:
            with patch("app.modules.scheduler.get_scheduler", return_value=scheduler), patch.object(
                handlers, "get_bot", return_value=Mock()
            ), patch.object(handlers, "send") as send_mock:
                handlers._do_sync("123")
        finally:
            if handlers._task_lock.locked():
                handlers._task_lock.release()

        event = send_mock.call_args.args[0]
        self.assertEqual(dict(event.fields)["状态"], "⚠️ 部分完成")
        self.assertTrue(any("为避免误删" in line for line in event.lines))


class _FailingTreeClient:
    def list_dir(self, dir_id):
        if dir_id == "root":
            return [GuangYaFile("broken", "坏目录", True)]
        raise RuntimeError("目录连接超时 token=secret")


class StrmDetailedNotificationTests(IsolatedDatabaseTestCase):
    def test_sync_stats_include_directories_timing_and_error_samples(self):
        with tempfile.TemporaryDirectory() as root, patch(
            "app.modules.strm.db.list_strm_index", return_value=[]
        ):
            stats = sync_strm(
                "root", "http://media", root, client=_FailingTreeClient(),
                clean_invalid=False,
            )

        self.assertEqual(stats["directories"], 1)
        self.assertIn("scan_elapsed_seconds", stats)
        self.assertIn("metadata_elapsed_seconds", stats)
        self.assertTrue(stats["error_samples"])
        self.assertNotIn("secret", " ".join(stats["error_samples"]))

    def test_strm_event_contains_source_change_cleanup_refresh_and_timing(self):
        event = STRMScheduler._build_success_event(
            stats={
                "directories": 3, "total": 8, "generated": 2, "skipped": 6,
                "failed": 0, "metadata_generated": 4, "metadata_skipped": 2,
                "metadata_failed": 0, "metadata_cleaned": 1, "cleaned": 2,
                "empty_dirs_cleaned": 1, "scan_elapsed_seconds": 1.2,
                "metadata_elapsed_seconds": 0.8,
                "error_samples": ["电影/坏目录：连接超时"],
            },
            refresh={"Emby": "queued", "Jellyfin": "failed"},
            elapsed=2.4,
            trigger_type="organize",
            sources=[{"id": "source-1", "name": "电影", "stats": {}}],
            strm_root="/data/strm",
        )
        rendered = notifier.render_event(event)

        self.assertIn("STRM 同步部分完成", rendered)
        for text in ("同步来源", "扫描范围", "STRM 变化", "元数据", "清理", "媒体库刷新", "总耗时"):
            self.assertIn(text, rendered)
        self.assertIn("电影/坏目录：连接超时", rendered)
        self.assertIn("Emby 已排队", rendered)
        self.assertIn("Jellyfin ❌", rendered)


class _PreviewScraper:
    match_mode = "strict"

    def parse_resource_tags(self, filename):
        return TMDBScraper.parse_resource_tags(filename)

    def parse_media(self, filename, parent_path="", match=None):
        return release_parse_result(
            {"title": "The Furious", "year": "2026", "type": "movie", "season": None, "episode": None},
            filename=filename, parent_path=parent_path,
        )

    def match(self, filename):
        return MatchResult(
            tmdb_id="1510055", title="狂怒者：荣誉之战", year="2026",
            media_type="movie", confidence=0.86, need_confirm=True,
            error="匹配置信度 86% 低于严格模式阈值 90%",
            status="low_confidence", matched_by="search", threshold=0.9,
            candidates=[Candidate(
                "1510055", "狂怒者：荣誉之战", "2026", 0.86, "movie",
                original_title="The Furious", overview="候选简介",
                poster_path="/candidate.jpg", release_date="2026-05-01",
            )],
        )

    def get_detail(self, tmdb_id, media_type):
        return {
            "id": 1510055, "title": "狂怒者：荣誉之战",
            "original_title": "The Furious", "release_date": "2026-05-01",
            "overview": "电影简介", "vote_average": 7.8, "vote_count": 120,
            "status": "Released", "poster_path": "/poster.jpg",
            "backdrop_path": "/backdrop.jpg",
            "genres": [{"name": "动作"}],
            "production_countries": [{"name": "中国"}],
            "spoken_languages": [{"name": "普通话"}],
            "production_companies": [{"name": "测试影业"}],
        }


class ScrapePreviewApiTests(unittest.TestCase):
    def test_resource_tags_parse_release_tokens(self):
        tags = TMDBScraper.parse_resource_tags(
            "Movie.2026.2160p.iTunes.WEB-DL.DDP5.1.Atmos.HDR10+.H.265-DreamHD.mkv"
        )

        self.assertEqual(tags["resolution"], "2160p")
        self.assertEqual(tags["source"], "iTunes")
        self.assertEqual(tags["media"], "WEB-DL")
        self.assertEqual(tags["video_codec"], "H.265")
        self.assertIn("Atmos", tags["audio"])
        self.assertIn("HDR10+", tags["effect"])
        self.assertEqual(tags["release_group"], "DreamHD")

    def test_resource_tags_do_not_misclassify_web_dl_suffix_as_release_group(self):
        tags = TMDBScraper.parse_resource_tags(
            "The.Office.S01E01.1080p.WEB-DL.mkv"
        )

        self.assertEqual(tags["media"], "WEB-DL")
        self.assertEqual(tags["release_group"], "")

    def test_preview_returns_diagnostic_tmdb_detail_and_rich_candidates(self):
        from app.routes import tools_api

        with patch.object(tools_api, "require_api_login", return_value=None), patch.object(
            tools_api, "TMDBScraper", return_value=_PreviewScraper()
        ):
            response = tools_api.scrape_preview(Mock(), {
                "filename": "The.Furious.2026.2160p.WEB-DL.H.265-DreamHD.mkv"
            })

        payload = json.loads(response.body)
        self.assertEqual(payload["diagnostic"]["status"], "low_confidence")
        self.assertEqual(payload["diagnostic"]["threshold"], 0.9)
        self.assertEqual(payload["match"]["original_title"], "The Furious")
        self.assertEqual(payload["match"]["overview"], "电影简介")
        self.assertTrue(payload["match"]["poster_url"].endswith("/poster.jpg"))
        self.assertEqual(payload["candidates"][0]["overview"], "候选简介")
        self.assertIn("resource_tags", payload["parsed"])
        self.assertEqual(payload["parsed"]["resource_tags"]["resolution"], "2160p")


class ScrapePreviewTemplateTests(unittest.TestCase):
    def test_logs_template_has_rich_regions_and_safe_dom_helpers(self):
        html = (Path("app/templates/logs.html").read_text(encoding="utf-8") + Path("app/static/js/logs.js").read_text(encoding="utf-8"))
        for marker in (
            "scrapeHero", "scrapeDiagnostic", "scrapeMetadata",
            "scrapeResourceTags", "scrapeCandidates", "scrapeNaming",
        ):
            self.assertIn(marker, html)
        self.assertIn("function scrapeText", html)
        self.assertIn("textContent", html)
        self.assertIn("replaceChildren", html)

    def test_scrape_preview_css_reserves_media_and_result_space(self):
        css = Path("app/static/css/scrape-preview.css").read_text(encoding="utf-8")
        self.assertIn(".scrape-lab", css)
        self.assertIn(".scrape-lab [hidden]", css)
        self.assertIn("display: none !important", css)
        self.assertIn("min-height", css)
        self.assertIn("aspect-ratio", css)
        self.assertIn("@media", css)
        self.assertIn("prefers-reduced-motion", css)

    def test_operation_history_uses_compact_conditional_sections(self):
        html = (Path("app/templates/logs.html").read_text(encoding="utf-8") + Path("app/static/js/logs.js").read_text(encoding="utf-8"))
        css = Path("app/static/css/main.css").read_text(encoding="utf-8")
        self.assertIn('id="organizeOperationSection" hidden', html)
        self.assertIn('id="organizeDeleteAuditSection" hidden', html)
        self.assertIn("操作记录", html)
        self.assertIn("删除记录", html)
        self.assertNotIn("OPERATION HISTORY", html)
        self.assertNotIn("RECYCLE-BIN AUDIT", html)
        self.assertNotIn("organize-operation-empty", html)
        self.assertNotIn(".organize-operation-empty", css)
        self.assertIn("min-height: 72px", css)
        self.assertIn(".organize-detail-dialog [hidden] { display: none !important; }", css)
        self.assertIn("namingPreview.replaceChildren();namingPreview.hidden=true", html)

    def test_base_supports_page_scoped_stylesheet(self):
        base = Path("app/templates/base.html").read_text(encoding="utf-8")
        logs = (Path("app/templates/logs.html").read_text(encoding="utf-8") + Path("app/static/js/logs.js").read_text(encoding="utf-8"))
        self.assertIn("{% block page_styles %}", base)
        self.assertIn("css/scrape-preview.css", logs)


class GuangYaFileInfoTests(unittest.TestCase):
    @staticmethod
    def _client_with_response(response):
        class Raw:
            token = ""
            token_expires_at = None

            def fs_detail(self, file_id):
                return response

        client = GuangYaClient.__new__(GuangYaClient)
        client._raw = Raw()
        client._last_persisted_token = ""
        return client

    def test_file_info_unwraps_real_data_file_info_response(self):
        client = self._client_with_response({
            "msg": "success",
            "data": {
                "fileInfo": {
                    "fileId": "1928176823132528739",
                    "fileName": "封神第二部.mkv",
                    "fileSize": 11870497080,
                    "gcid": "GCID-1",
                    "parentId": "1927445875113771071",
                    "resType": 1,
                }
            },
        })

        item = client.file_info("1928176823132528739")

        self.assertIsNotNone(item)
        self.assertEqual(item.file_id, "1928176823132528739")
        self.assertEqual(item.parent_id, "1927445875113771071")
        self.assertEqual(item.name, "封神第二部.mkv")
        self.assertEqual(item.size, 11870497080)
        self.assertEqual(item.etag, "GCID-1")

    def test_file_info_rejects_success_wrapper_without_valid_file(self):
        client = self._client_with_response({"msg": "success", "data": {"fileInfo": {}}})
        self.assertIsNone(client.file_info("missing"))

    def test_reorganize_notification_disabled_is_silent_not_a_warning(self):
        from types import SimpleNamespace
        from app.modules.organize_correction import OrganizeCorrectionService

        service = SimpleNamespace()
        rules = SimpleNamespace(notify_enabled=False, library_notify=True)
        with patch(
            "app.modules.telegram_notification_center.publish_notification_event"
        ) as publisher:
            warnings = OrganizeCorrectionService._notify_reorganize_result(
                service, {}, [], rules,
            )

        self.assertEqual(warnings, [])
        publisher.assert_not_called()

    def test_snapshot_validation_reports_unreadable_detail_before_position_drift(self):
        from app.modules.organize_correction import CorrectionItem, OrganizeCorrectionService

        client = Mock()
        client.file_info.return_value = GuangYaFile("", "", False, parent_id="0")
        service = OrganizeCorrectionService(client=client, scraper=Mock())
        item = CorrectionItem(
            1, "video", "video", "source", "Movie.mkv",
            "source", "Movie.mkv", 100, "etag",
        )

        with self.assertRaisesRegex(RuntimeError, "无法读取云端文件详情"):
            service._verify_item(item)


if __name__ == "__main__":
    unittest.main()
