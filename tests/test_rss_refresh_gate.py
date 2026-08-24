from __future__ import annotations

import re
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app import database as db
from app.bot import handlers
from app.main import create_app
from app.modules.rss import (
    RSS_REFRESH_BUSY_ERROR,
    RSS_REFRESH_CONFLICT_ERROR,
    MikanParser,
    RSSEngine,
    rss_subscription_refresh_revision,
)
from app.modules.rss_scheduler import RSSScheduler
from tests.support import IsolatedDatabaseTestCase


class RSSRefreshGateTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM rss_entries")
            conn.execute("DELETE FROM rss_items")

    @staticmethod
    def _subscription(suffix: str) -> int:
        return db.add_rss_subscription(
            f"subscription-{suffix}",
            f"https://example.invalid/{suffix}",
        )

    def test_same_subscription_second_refresh_returns_busy_without_waiting(self):
        sid = self._subscription("same")
        started = threading.Event()
        release = threading.Event()
        first_result: dict[str, dict] = {}
        first = RSSEngine()
        second = RSSEngine()

        def blocking_parse(_url):
            started.set()
            self.assertTrue(release.wait(2))
            return []

        first.parser.parse = Mock(side_effect=blocking_parse)
        second.parser.parse = Mock(return_value=[])
        thread = threading.Thread(
            target=lambda: first_result.setdefault("value", first.refresh(sid)),
        )
        thread.start()
        self.assertTrue(started.wait(1))
        try:
            self.assertEqual(second.refresh(sid), {
                "error": RSS_REFRESH_BUSY_ERROR,
                "busy": True,
            })
            second.parser.parse.assert_not_called()
        finally:
            release.set()
            thread.join(2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(first_result["value"], {"total": 0, "new": 0, "skipped": 0})

    def test_different_subscriptions_can_refresh_in_parallel(self):
        first_sid = self._subscription("first")
        second_sid = self._subscription("second")
        started = threading.Event()
        release = threading.Event()
        first = RSSEngine()
        second = RSSEngine()
        first.parser.parse = Mock(side_effect=lambda _url: (started.set(), release.wait(2), [])[2])
        second.parser.parse = Mock(return_value=[])
        thread = threading.Thread(target=lambda: first.refresh(first_sid))
        thread.start()
        self.assertTrue(started.wait(1))
        try:
            self.assertEqual(second.refresh(second_sid), {"total": 0, "new": 0, "skipped": 0})
            second.parser.parse.assert_called_once()
        finally:
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_revision_is_rechecked_after_gate_acquisition(self):
        sid = self._subscription("revision")
        expected = rss_subscription_refresh_revision(db.get_rss_subscription(sid))
        engine = RSSEngine()
        engine.parser.parse = Mock(return_value=[])

        def acquire_after_change(_sub_id):
            db.update_rss_subscription(sid, {
                "urls": "https://changed.invalid/revision",
            })
            return True

        with patch("app.modules.rss._try_acquire_rss_refresh", side_effect=acquire_after_change):
            result = engine.refresh(sid, expected_revision=expected)

        self.assertEqual(result, {
            "error": RSS_REFRESH_CONFLICT_ERROR,
            "conflict": True,
        })
        engine.parser.parse.assert_not_called()

    def test_gate_is_released_after_refresh_exception(self):
        sid = self._subscription("exception")
        failed = RSSEngine()
        failed.parser.parse = Mock(side_effect=RuntimeError("upstream secret"))
        with self.assertRaises(RuntimeError):
            failed.refresh(sid)

        recovered = RSSEngine()
        recovered.parser.parse = Mock(return_value=[])
        self.assertEqual(recovered.refresh(sid), {"total": 0, "new": 0, "skipped": 0})
        recovered.parser.parse.assert_called_once()

    def test_all_failed_sources_do_not_advance_last_refresh_time(self):
        sid = self._subscription("all-failed")
        db.update_rss_subscription(sid, {
            "urls": "https://example.invalid/a\nhttps://example.invalid/b",
        })
        engine = RSSEngine()

        def failed_parse(_url):
            engine.parser.last_error_code = "fetch_failed"
            return []

        engine.parser.parse = Mock(side_effect=failed_parse)
        result = engine.refresh(sid)

        self.assertEqual(result["error_code"], "all_sources_failed")
        self.assertEqual(result["failed_sources"], 2)
        self.assertIsNone(db.get_rss_subscription(sid)["last_refreshed_at"])

    def test_valid_empty_feed_advances_last_refresh_time(self):
        sid = self._subscription("valid-empty")
        engine = RSSEngine()

        def empty_parse(_url):
            engine.parser.last_error_code = ""
            return []

        engine.parser.parse = Mock(side_effect=empty_parse)
        self.assertEqual(engine.refresh(sid), {"total": 0, "new": 0, "skipped": 0})
        self.assertTrue(db.get_rss_subscription(sid)["last_refreshed_at"])

    def test_scheduler_bounds_concurrent_refresh_threads(self):
        rows = [
            {"id": index, "action": "subscribe"}
            for index in range(1, 8)
        ]
        started = []

        class FakeThread:
            def __init__(self, *, target, args, name, daemon):
                self.target = target
                self.args = args
                self.name = name
                self.daemon = daemon

            def start(self):
                started.append(self)

        scheduler = RSSScheduler()
        with patch("app.modules.rss_scheduler.db.list_due_rss_subscriptions", return_value=rows), \
                patch("app.modules.rss_scheduler.threading.Thread", FakeThread), \
                patch.object(scheduler, "_run_cleanup_if_due", return_value=0):
            self.assertEqual(scheduler.run_due(), 4)

        self.assertEqual(len(started), 4)
        self.assertEqual(scheduler._running_ids, {1, 2, 3, 4})

    def test_scheduler_runs_stale_submission_recovery_each_cycle(self):
        scheduler = RSSScheduler()
        with patch.object(scheduler, "_run_cleanup_if_due"), patch(
            "app.modules.rss_scheduler.db.recover_stale_submitting_rss_entries",
            return_value=0,
        ) as recover, patch(
            "app.modules.rss_scheduler.db.list_due_rss_subscriptions", return_value=[]
        ):
            self.assertEqual(scheduler.run_due(), 0)

        recover.assert_called_once_with(stale_minutes=15)

    def test_scheduler_stop_waits_for_active_refresh_workers(self):
        scheduler = RSSScheduler()
        release = threading.Event()
        worker = threading.Thread(target=release.wait, name="rss-refresh-test", daemon=True)
        scheduler._workers[1] = worker
        scheduler._running_ids.add(1)
        worker.start()

        self.assertFalse(scheduler.stop(timeout=0.01))
        release.set()
        worker.join(timeout=1)
        self.assertTrue(scheduler.stop(timeout=0.1))

    def test_scheduler_stop_closes_worker_admission_during_due_query(self):
        scheduler = RSSScheduler()
        entered = threading.Event()
        release = threading.Event()
        results = []

        def delayed_due_rows():
            entered.set()
            release.wait(timeout=2)
            return [{"id": 9, "action": "download"}]

        with patch.object(scheduler, "_run_cleanup_if_due", return_value=0), patch(
            "app.modules.rss_scheduler.db.recover_stale_submitting_rss_entries",
            return_value=0,
        ), patch(
            "app.modules.rss_scheduler.db.list_due_rss_subscriptions",
            side_effect=delayed_due_rows,
        ), patch.object(scheduler, "_execute") as execute:
            runner = threading.Thread(target=lambda: results.append(scheduler.run_due()))
            runner.start()
            self.assertTrue(entered.wait(timeout=1))
            self.assertTrue(scheduler.stop(timeout=0.01))
            release.set()
            runner.join(timeout=1)

        self.assertFalse(runner.is_alive())
        self.assertEqual(results, [0])
        execute.assert_not_called()
        self.assertEqual(scheduler._running_ids, set())
        self.assertEqual(scheduler._workers, {})

    def test_scheduler_cancels_queued_refresh_when_subscription_is_paused(self):
        sid = db.add_rss_subscription(
            "queued-pause",
            "https://example.invalid/queued-pause",
            refresh_interval_minutes=10,
        )
        queued = []

        class FakeThread:
            def __init__(self, *, target, args, name, daemon):
                self.target = target
                self.args = args

            def start(self):
                queued.append(self)

        scheduler = RSSScheduler()
        row = db.get_rss_subscription(sid)
        with patch(
            "app.modules.rss_scheduler.db.list_due_rss_subscriptions", return_value=[row]
        ), patch(
            "app.modules.rss_scheduler.threading.Thread", FakeThread
        ), patch.object(
            scheduler, "_run_cleanup_if_due", return_value=0
        ), patch(
            "app.modules.rss_scheduler.db.recover_stale_submitting_rss_entries", return_value=0
        ):
            self.assertEqual(scheduler.run_due(), 1)

        db.update_rss_subscription(sid, {"enabled": 0})
        with patch("app.modules.rss.MikanParser.parse") as parse:
            queued[0].target(*queued[0].args)

        parse.assert_not_called()
        self.assertNotIn(sid, scheduler._running_ids)

    def test_parser_logs_do_not_expose_private_feed_url_or_exception(self):
        private_url = "https://secret.invalid/rss?passkey=RSS_SECRET"
        with patch("app.modules.rss.requests.get", side_effect=RuntimeError(
            "failed https://secret.invalid/rss?passkey=RSS_SECRET"
        )), self.assertLogs("app.modules.rss", level="ERROR") as captured:
            self.assertEqual(MikanParser().parse(private_url), [])

        serialized = "\n".join(captured.output)
        self.assertIn("RSS 拉取失败", serialized)
        for secret in ("secret.invalid", "passkey", "RSS_SECRET"):
            self.assertNotIn(secret, serialized)

    def test_parser_uses_bounded_http_timeouts(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.raise_for_status = Mock()
        response.iter_content = Mock(return_value=[b"<rss><channel></channel></rss>"])
        response.headers = {"content-type": "application/rss+xml"}
        response.url = "https://example.invalid/feed"
        parsed = SimpleNamespace(bozo=False, entries=[])

        with patch("app.modules.rss.requests.get", return_value=response) as get, patch(
            "app.modules.rss.feedparser.parse", return_value=parsed
        ) as parse:
            self.assertEqual(MikanParser().parse(response.url), [])

        get.assert_called_once_with(
            response.url,
            headers={"User-Agent": MikanParser.USER_AGENT},
            timeout=(10, 30),
            stream=True,
        )
        parse.assert_called_once()

    def test_auto_download_preserves_unknown_outcome_summary(self):
        sid = self._subscription("auto-unknown")
        engine = RSSEngine()
        with patch.object(
            engine, "refresh", return_value={"total": 1, "new": 1, "skipped": 0}
        ), patch(
            "app.modules.rss.db.list_rss_entries", return_value=[{"id": 17, "title": "普通资源"}]
        ), patch.object(
            engine,
            "download_many",
            return_value={
                "success_count": 0,
                "existing_count": 0,
                "unverified_count": 0,
                "failure_count": 1,
                "outcome_unknown_count": 1,
                "review_required": True,
            },
        ):
            result = engine.auto_download(sid)

        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["outcome_unknown_count"], 1)
        self.assertTrue(result["review_required"])

    def test_auto_download_applies_current_exclude_keywords_to_existing_pending_rows(self):
        sid = self._subscription("historical-filter")
        db.update_rss_subscription(sid, {"exclude_keywords": "合集"})
        # 历史普通条目可能没有 rss_entry_media；过滤时也必须补写可解释原因。
        excluded_id = db.add_rss_entry(
            sid,
            "作品 合集 01-12",
            "excluded-guid",
        )
        accepted_id = db.add_rss_entry(
            sid,
            "作品 S01E13",
            "accepted-guid",
        )
        engine = RSSEngine()
        with patch.object(
            engine, "refresh", return_value={"total": 2, "new": 0, "skipped": 0}
        ), patch.object(
            engine,
            "download_many",
            return_value={
                "success_count": 1,
                "existing_count": 0,
                "unverified_count": 0,
                "failure_count": 0,
                "outcome_unknown_count": 0,
                "review_required": False,
            },
        ) as download_many:
            result = engine.auto_download(sid)

        self.assertEqual(result["filtered"], 1)
        download_many.assert_called_once_with([accepted_id])
        excluded = db.get_rss_entry(int(excluded_id))
        self.assertEqual(excluded["status"], "skipped")
        self.assertTrue(excluded["processed"])
        with db.get_conn() as conn:
            media = conn.execute(
                "SELECT skip_reason FROM rss_entry_media WHERE rss_entry_id=?",
                (int(excluded_id),),
            ).fetchone()
        self.assertEqual(media["skip_reason"], "命中当前订阅排除关键词")

    def test_scheduler_deduplicates_identical_alerts_until_recovery(self):
        sid = self._subscription("scheduler-alert")
        scheduler = RSSScheduler()
        issue = {
            "refresh": {"total": 1, "new": 1, "skipped": 0},
            "downloaded": 0,
            "existing": 0,
            "unverified": 0,
            "failed": 1,
            "outcome_unknown_count": 1,
            "review_required": True,
        }
        healthy = {"total": 0, "new": 0, "skipped": 0}
        with patch("app.modules.rss_scheduler.RSSEngine") as engine, patch(
            "app.modules.rss_scheduler.send_event", side_effect=[False, True, True]
        ) as send_event:
            engine.return_value.auto_download.return_value = issue
            scheduler._execute(sid, "download")
            scheduler._execute(sid, "download")
            scheduler._execute(sid, "download")
            self.assertEqual(send_event.call_count, 2)
            engine.return_value.refresh.return_value = healthy
            scheduler._execute(sid, "subscribe")
            engine.return_value.auto_download.return_value = issue
            scheduler._execute(sid, "download")

        self.assertEqual(send_event.call_count, 3)

    def test_scheduler_retries_unresolved_alert_and_persists_delivery_signature(self):
        sid = self._subscription("scheduler-durable-alert")
        entry_id = db.add_rss_entry(sid, "待核对资源", "durable-alert-guid")
        self.assertIsNotNone(entry_id)
        db.record_rss_entry_failure(int(entry_id), "qb_outcome_unknown", False)
        issue = {
            "refresh": {"total": 1, "new": 1, "skipped": 0},
            "downloaded": 0,
            "existing": 0,
            "unverified": 0,
            "failed": 1,
            "outcome_unknown_count": 1,
            "review_required": True,
        }
        healthy = {"total": 0, "new": 0, "skipped": 0}
        scheduler = RSSScheduler()
        with patch("app.modules.rss_scheduler.RSSEngine") as engine, patch(
            "app.modules.rss_scheduler.send_event", side_effect=[False, True]
        ) as send_event:
            engine.return_value.auto_download.return_value = issue
            scheduler._execute(sid, "download")
            engine.return_value.refresh.return_value = healthy
            scheduler._execute(sid, "subscribe")

        self.assertEqual(send_event.call_count, 2)
        persisted = db.kv_get(scheduler._alert_key(sid), "")
        self.assertIn("outcome_unknown", persisted)

        restarted = RSSScheduler()
        with patch("app.modules.rss_scheduler.RSSEngine") as engine, patch(
            "app.modules.rss_scheduler.send_event"
        ) as send_event:
            engine.return_value.refresh.return_value = healthy
            restarted._execute(sid, "subscribe")
        send_event.assert_not_called()

        db.update_rss_entries_processed([int(entry_id)], True)
        with patch("app.modules.rss_scheduler.RSSEngine") as engine:
            engine.return_value.refresh.return_value = healthy
            restarted._execute(sid, "subscribe")
        self.assertEqual(db.kv_get(restarted._alert_key(sid), "missing"), "")

    def test_scheduler_warns_when_auto_download_outcome_is_unknown(self):
        scheduler = RSSScheduler()
        with patch("app.modules.rss_scheduler.RSSEngine") as engine:
            engine.return_value.auto_download.return_value = {
                "refresh": {"total": 1, "new": 1, "skipped": 0},
                "downloaded": 0,
                "existing": 0,
                "unverified": 0,
                "failed": 1,
                "outcome_unknown_count": 1,
                "review_required": True,
            }
            with self.assertLogs(
                "app.modules.rss_scheduler", level="WARNING"
            ) as captured:
                scheduler._execute(7, "download")

        output = "\n".join(captured.output)
        self.assertIn("结果待核对", output)
        self.assertIn("outcome_unknown=1", output)

    def test_scheduler_treats_busy_as_skip(self):
        scheduler = RSSScheduler()
        with patch("app.modules.rss_scheduler.RSSEngine") as engine:
            engine.return_value.refresh.return_value = {
                "error": RSS_REFRESH_BUSY_ERROR,
                "busy": True,
            }
            with self.assertLogs("app.modules.rss_scheduler", level="DEBUG") as captured:
                scheduler._execute(7, "subscribe")
        output = "\n".join(captured.output)
        self.assertIn("刷新跳过", output)
        self.assertNotIn("WARNING", output)


class RSSRefreshSurfaceTests(IsolatedDatabaseTestCase):
    class FakeBot:
        def __init__(self):
            self.message_handlers = []
            self.callback_handlers = []
            self.replies = []
            self.answers = []
            self.commands = []

        def message_handler(self, **filters):
            def decorate(handler):
                self.message_handlers.append((filters, handler))
                return handler
            return decorate

        def callback_query_handler(self, **filters):
            def decorate(handler):
                self.callback_handlers.append((filters, handler))
                return handler
            return decorate

        def reply_to(self, message, text, **kwargs):
            self.replies.append((message, text, kwargs))

        def answer_callback_query(self, *args, **kwargs):
            self.answers.append((args, kwargs))

        def set_my_commands(self, commands):
            self.commands = commands

    class AsyncFakeBot(FakeBot):
        def __init__(self):
            super().__init__()
            self.messages = []
            self.edits = []
            self.actions = []

        def send_chat_action(self, chat_id, action):
            self.actions.append((chat_id, action))

        def send_message(self, chat_id, text, **kwargs):
            self.messages.append((chat_id, text, kwargs))
            return SimpleNamespace(message_id=31)

        def edit_message_text(self, text, chat_id, message_id, **kwargs):
            self.edits.append((chat_id, message_id, text, kwargs))

    class ImmediateThread:
        def __init__(self, *, target, args=(), name="", **_kwargs):
            self.target = target
            self.args = args
            self.name = name

        def start(self):
            if str(self.name).startswith("tg-progress-"):
                return
            self.target(*self.args)

    class FailingThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("cannot start thread")

    @staticmethod
    def _telebot_types():
        class Markup:
            def __init__(self, row_width=2):
                self.row_width = row_width
                self.buttons = []

            def add(self, *buttons):
                self.buttons.extend(buttons)

        return SimpleNamespace(types=SimpleNamespace(
            InlineKeyboardMarkup=Markup,
            InlineKeyboardButton=lambda text, callback_data: SimpleNamespace(
                text=text, callback_data=callback_data,
            ),
            BotCommand=lambda command, description: SimpleNamespace(
                command=command, description=description,
            ),
        ))

    @staticmethod
    def _csrf(html: str) -> str:
        match = re.search(r'name="csrf_token"\s+value="([^"]+)"', html)
        if not match:
            match = re.search(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', html)
        if not match:
            raise AssertionError("CSRF token missing")
        return match.group(1)

    def test_web_refresh_and_auto_return_conflict_while_busy(self):
        with TestClient(create_app(start_background=False)) as client:
            page = client.get("/login")
            login = client.post("/login", data={
                "username": "admin",
                "password": "123456",
                "csrf_token": self._csrf(page.text),
            }, follow_redirects=False)
            self.assertEqual(login.status_code, 302)
            csrf = self._csrf(client.get("/settings").text)
            headers = {"X-CSRF-Token": csrf}
            busy = {"error": RSS_REFRESH_BUSY_ERROR, "busy": True}
            with patch("app.routes.rss_api.RSSEngine") as engine:
                engine.return_value.refresh.return_value = busy
                engine.return_value.auto_download.return_value = busy
                refresh = client.post("/api/rss/subscriptions/7/refresh", headers=headers)
                auto = client.post("/api/rss/subscriptions/7/auto", headers=headers)
        self.assertEqual(refresh.status_code, 409)
        self.assertTrue(refresh.json()["busy"])
        self.assertEqual(auto.status_code, 409)
        self.assertTrue(auto.json()["busy"])

    def test_web_refresh_exception_is_sanitized(self):
        secret = "https://secret.invalid/rss?passkey=RSS_SECRET"
        with TestClient(create_app(start_background=False)) as client:
            page = client.get("/login")
            login = client.post("/login", data={
                "username": "admin",
                "password": "123456",
                "csrf_token": self._csrf(page.text),
            }, follow_redirects=False)
            self.assertEqual(login.status_code, 302)
            csrf = self._csrf(client.get("/settings").text)
            with patch("app.routes.rss_api.RSSEngine") as engine:
                engine.return_value.refresh.side_effect = RuntimeError(secret)
                response = client.post(
                    "/api/rss/subscriptions/7/refresh",
                    headers={"X-CSRF-Token": csrf},
                )
        self.assertEqual(response.status_code, 500)
        self.assertNotIn(secret, response.text)
        self.assertNotIn("passkey", response.text.casefold())

    def test_telegram_rss_commands_without_ids_reply_with_actionable_choices(self):
        bot = self.FakeBot()
        with patch(
            "app.bot.handlers.get",
            side_effect=lambda key, default="": "100" if key == "TG_CHAT_ID" else default,
        ), patch("app.database.list_rss_subscriptions", return_value=[{
            "id": 1, "name": "mikanani", "enabled": 1,
        }]), patch("app.database.list_rss_entries", return_value=[{
            "id": 9, "title": "示例 RSS 条目",
        }]):
            handlers._register_commands(bot, self._telebot_types())
            refresh_handler = next(
                handler for filters, handler in bot.message_handlers
                if filters.get("commands") == ["rss_refresh"]
            )
            download_handler = next(
                handler for filters, handler in bot.message_handlers
                if filters.get("commands") == ["rss_dl"]
            )
            refresh_handler(SimpleNamespace(
                text="/rss_refresh", chat=SimpleNamespace(id=100), message_id=1,
            ))
            download_handler(SimpleNamespace(
                text="/rss_dl", chat=SimpleNamespace(id=100), message_id=2,
            ))

        self.assertIn("请选择要刷新的订阅", bot.replies[0][1])
        self.assertIn("/rss_refresh 1", bot.replies[0][1])
        self.assertIn("mikanani", bot.replies[0][1])
        self.assertIn("请选择要下载的条目", bot.replies[1][1])
        self.assertIn("/rss_dl 9", bot.replies[1][1])
        self.assertIn("示例 RSS 条目", bot.replies[1][1])

    def test_telegram_rss_command_fallback_matches_bot_suffix(self):
        bot = self.FakeBot()
        with patch(
            "app.bot.handlers.get",
            side_effect=lambda key, default="": "100" if key == "TG_CHAT_ID" else default,
        ), patch("app.database.list_rss_subscriptions", return_value=[]):
            handlers._register_commands(bot, self._telebot_types())
            fallback_filter, fallback_handler = next(
                (filters, handler) for filters, handler in bot.message_handlers
                if filters.get("content_types") == ["text"]
                and callable(filters.get("func"))
                and filters["func"](SimpleNamespace(text="/rss_refresh@MediaFluxBot"))
            )
            self.assertTrue(fallback_filter["func"](
                SimpleNamespace(text="/rss_refresh@MediaFluxBot")
            ))
            fallback_handler(SimpleNamespace(
                text="/rss_refresh@MediaFluxBot",
                chat=SimpleNamespace(id=100),
                message_id=3,
            ))

        self.assertIn("暂无 RSS 订阅", bot.replies[-1][1])

    def test_telegram_rss_commands_require_confirmation_then_finish_in_place(self):
        from app.modules.telegram_write_confirmations import (
            reset_telegram_write_confirmation_store_for_tests,
        )

        reset_telegram_write_confirmation_store_for_tests()
        bot = self.AsyncFakeBot()
        telebot = self._telebot_types()
        subscription = {
            "id": 7,
            "name": "mikanani",
            "enabled": 1,
            "updated_at": "2026-08-09 12:00:00",
            "urls": "https://example.invalid/rss",
            "parser": "mikan",
            "exclude_keywords": "",
            "action": "subscribe",
        }
        expected_revision = rss_subscription_refresh_revision(subscription)
        entry = {"id": 9, "title": "示例 RSS 条目"}
        with patch(
            "app.bot.handlers.get",
            side_effect=lambda key, default="": "100" if key == "TG_CHAT_ID" else default,
        ), patch("app.bot.handlers.threading.Thread", self.ImmediateThread), patch(
            "app.database.get_rss_subscription", return_value=subscription,
        ), patch("app.database.get_rss_entry", return_value=entry), patch(
            "app.modules.rss.RSSEngine.refresh",
            return_value={"total": 8, "new": 3, "skipped": 5},
        ) as refresh, patch(
            "app.modules.rss.RSSEngine.download",
            return_value={"ok": True, "method": "qBittorrent"},
        ) as download:
            handlers._register_commands(bot, telebot)
            refresh_handler = next(
                handler for filters, handler in bot.message_handlers
                if filters.get("commands") == ["rss_refresh"]
            )
            download_handler = next(
                handler for filters, handler in bot.message_handlers
                if filters.get("commands") == ["rss_dl"]
            )
            confirmation_handler = next(
                handler for filters, handler in bot.callback_handlers
                if filters["func"](SimpleNamespace(data="tgc:opaque"))
            )
            refresh_handler(SimpleNamespace(
                text="/rss_refresh 7",
                chat=SimpleNamespace(id=100),
                from_user=SimpleNamespace(id=9),
                message_id=1,
            ))
            self.assertIn("确认刷新 RSS 订阅", bot.replies[0][1])
            refresh.assert_not_called()
            refresh_callback = bot.replies[0][2]["reply_markup"].buttons[0].callback_data
            confirmation_handler(SimpleNamespace(
                id="refresh-confirm",
                data=refresh_callback,
                from_user=SimpleNamespace(id=9),
                message=SimpleNamespace(chat=SimpleNamespace(id=100), message_id=11),
            ))

            download_handler(SimpleNamespace(
                text="/rss_dl 9",
                chat=SimpleNamespace(id=100),
                from_user=SimpleNamespace(id=9),
                message_id=2,
            ))
            self.assertIn("确认提交 RSS 下载", bot.replies[1][1])
            download.assert_not_called()
            download_callback = bot.replies[1][2]["reply_markup"].buttons[0].callback_data
            confirmation_handler(SimpleNamespace(
                id="download-confirm",
                data=download_callback,
                from_user=SimpleNamespace(id=9),
                message=SimpleNamespace(chat=SimpleNamespace(id=100), message_id=12),
            ))

        refresh.assert_called_once_with(7, expected_revision=expected_revision)
        download.assert_called_once_with(9)
        self.assertIn("正在刷新 RSS 订阅", bot.messages[0][1])
        edit_texts = [row[2] for row in bot.edits]
        self.assertTrue(any("RSS 刷新已确认" in text for text in edit_texts))
        self.assertTrue(any("RSS 刷新完成" in text for text in edit_texts))
        self.assertIn("正在提交 RSS 下载", bot.messages[1][1])
        self.assertTrue(any("RSS 下载已确认" in text for text in edit_texts))
        download_result = next(text for text in edit_texts if "RSS 下载已提交" in text)
        self.assertIn("qBittorrent", download_result)
        confirmation_edits = [row for row in bot.edits if "已确认" in row[2]]
        self.assertTrue(confirmation_edits)
        self.assertTrue(all(row[3].get("reply_markup") is None for row in confirmation_edits))

    def test_telegram_busy_reply_is_not_reported_as_failure(self):
        bot = self.FakeBot()
        msg = SimpleNamespace(text="/rss_refresh 7", chat=SimpleNamespace(id=100))
        with patch("app.modules.rss.RSSEngine.refresh", return_value={
            "error": RSS_REFRESH_BUSY_ERROR,
            "busy": True,
        }):
            handlers._run_rss_refresh(bot, msg, 7, None)
        self.assertEqual(bot.replies[-1][1], RSS_REFRESH_BUSY_ERROR)
        self.assertNotIn("刷新失败", bot.replies[-1][1])

    def test_telegram_partial_refresh_and_unknown_download_are_explicit(self):
        bot = self.FakeBot()
        msg = SimpleNamespace(text="/rss_refresh 7", chat=SimpleNamespace(id=100))
        with patch("app.modules.rss.RSSEngine.refresh", return_value={
            "total": 8, "new": 3, "skipped": 5,
            "partial": True, "failed_sources": 2,
        }):
            handlers._run_rss_refresh(bot, msg, 7, None)
        refresh_text = bot.replies[-1][1]
        self.assertIn("RSS 刷新部分完成", refresh_text)
        self.assertIn("暂不可用源：2", refresh_text)

        with patch("app.modules.rss.RSSEngine.download", return_value={
            "ok": False,
            "error": "提交结果待核对，请先检查下载器状态，勿直接重复提交",
            "review_required": True,
        }):
            handlers._run_rss_download(bot, msg, 9, None)
        download_text = bot.replies[-1][1]
        self.assertIn("RSS 下载结果待核对", download_text)
        self.assertIn("勿直接重复提交", download_text)
        self.assertNotIn("提交失败", download_text)

    def test_telegram_refresh_exception_is_sanitized(self):
        bot = self.FakeBot()
        msg = SimpleNamespace(text="/rss_refresh 7", chat=SimpleNamespace(id=100))
        with patch(
            "app.modules.rss.RSSEngine.refresh",
            side_effect=RuntimeError("https://secret.invalid/rss?passkey=RSS_SECRET"),
        ):
            handlers._run_rss_refresh(bot, msg, 7, None)
        self.assertEqual(bot.replies[-1][1], "刷新失败，请稍后重试")
        self.assertNotIn("passkey", bot.replies[-1][1].casefold())


    def test_unified_organize_command_selects_scope_and_starts_local_workflow(self):
        from app.modules.telegram_write_confirmations import (
            reset_telegram_write_confirmation_store_for_tests,
        )

        reset_telegram_write_confirmation_store_for_tests()
        bot = self.AsyncFakeBot()
        telebot = self._telebot_types()
        values = {"TG_CHAT_ID": "100", "GY_ORGANIZE_TARGET_DIR": "target-id"}
        with patch(
            "app.bot.handlers.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch(
            "app.bot.handlers._maintenance_task_busy", return_value=False,
        ), patch(
            "app.bot.handlers._configured_organize_sources",
            return_value=[{"id": "source-id", "name": "动画"}],
        ), patch(
            "app.bot.handlers._configured_local_organize_sources",
            return_value=[SimpleNamespace(id=7, name="qB 下载")],
        ), patch(
            "app.bot.handlers._start_organize_local", return_value=True,
        ) as start_local:
            handlers._register_commands(bot, telebot)
            command = next(
                handler for filters, handler in bot.message_handlers
                if filters.get("commands") == ["organize"]
            )
            confirmation = next(
                handler for filters, handler in bot.callback_handlers
                if filters["func"](SimpleNamespace(data="tgc:opaque"))
            )
            message = SimpleNamespace(
                text="/organize", chat=SimpleNamespace(id=100),
                from_user=SimpleNamespace(id=9), message_id=4,
            )
            command(message)

            self.assertIn("选择整理范围", bot.replies[-1][1])
            buttons = bot.replies[-1][2]["reply_markup"].buttons
            self.assertEqual(
                [button.text for button in buttons],
                ["光鸭云盘", "本地下载", "全部整理", "取消"],
            )
            local_button = next(button for button in buttons if button.text == "本地下载")
            callback_message = SimpleNamespace(chat=SimpleNamespace(id=100), message_id=14)
            confirmation(SimpleNamespace(
                id="organize-local", data=local_button.callback_data,
                from_user=SimpleNamespace(id=9), message=callback_message,
            ))

        start_local.assert_called_once_with(bot, telebot, callback_message)
        self.assertTrue(any("本地下载整理已确认" in row[2] for row in bot.edits))

    def test_all_organize_runs_cloud_then_local_and_finishes_once(self):
        progress = Mock()
        order = []
        cloud_state = {"status": "completed", "stats": {"moved": 2}}
        local_summary = {
            "completed": 1, "requires_manual": 0, "failed": 0,
            "moved_items": 3, "source_errors": [],
        }
        with patch(
            "app.bot.handlers._run_guangya_organize_stage",
            side_effect=lambda *args, **kwargs: (order.append("cloud"), cloud_state)[1],
        ), patch(
            "app.bot.handlers._run_local_organize_stage",
            side_effect=lambda *args, **kwargs: (order.append("local"), local_summary)[1],
        ), patch(
            "app.bot.handlers.render_event", return_value="combined-result",
        ), patch(
            "app.bot.handlers.send",
        ) as send:
            handlers._organize_running = True
            handlers._local_organize_running = True
            handlers._do_organize_all(
                100, [{"id": "source-id", "name": "动画"}], "target-id", progress,
            )

        self.assertEqual(order, ["cloud", "local"])
        progress.finish.assert_called_once_with("combined-result")
        send.assert_not_called()
        self.assertFalse(handlers._organize_running)
        self.assertFalse(handlers._local_organize_running)

    def test_sync_and_organize_commands_require_owner_bound_confirmation(self):
        from app.modules.telegram_write_confirmations import (
            reset_telegram_write_confirmation_store_for_tests,
        )

        reset_telegram_write_confirmation_store_for_tests()
        bot = self.AsyncFakeBot()
        telebot = self._telebot_types()
        values = {
            "TG_CHAT_ID": "100",
            "GY_ORGANIZE_TARGET_DIR": "target-id",
        }
        with patch(
            "app.bot.handlers.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch(
            "app.bot.handlers._maintenance_task_busy", return_value=False
        ), patch(
            "app.bot.handlers._configured_organize_sources",
            return_value=[{"id": "source-id", "name": "动画"}],
        ), patch(
            "app.bot.handlers._start_sync_gy", return_value=True
        ) as start_sync, patch(
            "app.bot.handlers._start_organize_gy", return_value=True
        ) as start_organize:
            handlers._register_commands(bot, telebot)
            sync_handler = next(
                handler for filters, handler in bot.message_handlers
                if filters.get("commands") == ["sync_gy"]
            )
            organize_handler = next(
                handler for filters, handler in bot.message_handlers
                if filters.get("commands") == ["organize_gy"]
            )
            confirmation_handler = next(
                handler for filters, handler in bot.callback_handlers
                if filters["func"](SimpleNamespace(data="tgc:opaque"))
            )
            sync_message = SimpleNamespace(
                text="/sync_gy", chat=SimpleNamespace(id=100),
                from_user=SimpleNamespace(id=9), message_id=1,
            )
            organize_message = SimpleNamespace(
                text="/organize_gy", chat=SimpleNamespace(id=100),
                from_user=SimpleNamespace(id=9), message_id=2,
            )
            sync_handler(sync_message)
            organize_handler(organize_message)
            start_sync.assert_not_called()
            start_organize.assert_not_called()
            self.assertIn("确认同步光鸭 STRM", bot.replies[0][1])
            self.assertIn("确认执行光鸭整理", bot.replies[1][1])

            for index, expected_start in ((0, start_sync), (1, start_organize)):
                callback_data = bot.replies[index][2]["reply_markup"].buttons[0].callback_data
                callback_message = SimpleNamespace(
                    chat=SimpleNamespace(id=100), message_id=10 + index,
                )
                confirmation_handler(SimpleNamespace(
                    id=f"confirm-{index}", data=callback_data,
                    from_user=SimpleNamespace(id=9), message=callback_message,
                ))
                expected_start.assert_called_once_with(bot, telebot, callback_message)

            sealed = [row for row in bot.edits if "已确认" in row[2]]
            self.assertEqual(len(sealed), 2)
            self.assertTrue(all(row[3].get("reply_markup") is None for row in sealed))
            self.assertTrue(any("STRM 同步已确认" in row[2] for row in sealed))
            self.assertTrue(any("光鸭整理已确认" in row[2] for row in sealed))

    def test_sync_confirmation_cancel_never_starts_task(self):
        from app.modules.telegram_write_confirmations import (
            reset_telegram_write_confirmation_store_for_tests,
        )

        reset_telegram_write_confirmation_store_for_tests()
        bot = self.AsyncFakeBot()
        telebot = self._telebot_types()
        with patch(
            "app.bot.handlers.get",
            side_effect=lambda key, default="": "100" if key == "TG_CHAT_ID" else default,
        ), patch(
            "app.bot.handlers._maintenance_task_busy", return_value=False
        ), patch("app.bot.handlers._start_sync_gy") as start_sync:
            handlers._register_commands(bot, telebot)
            sync_handler = next(
                handler for filters, handler in bot.message_handlers
                if filters.get("commands") == ["sync_gy"]
            )
            confirmation_handler = next(
                handler for filters, handler in bot.callback_handlers
                if filters["func"](SimpleNamespace(data="tgc:opaque"))
            )
            sync_handler(SimpleNamespace(
                text="/sync_gy", chat=SimpleNamespace(id=100),
                from_user=SimpleNamespace(id=9), message_id=1,
            ))
            cancel_data = bot.replies[0][2]["reply_markup"].buttons[1].callback_data
            confirmation_handler(SimpleNamespace(
                id="cancel", data=cancel_data, from_user=SimpleNamespace(id=9),
                message=SimpleNamespace(chat=SimpleNamespace(id=100), message_id=11),
            ))

        start_sync.assert_not_called()
        self.assertEqual(bot.answers[-1][0][1], "操作已取消")
        self.assertIn("操作已取消", bot.edits[-1][2])

    def test_sync_confirmation_start_failure_closes_original_card(self):
        from app.modules.telegram_write_confirmations import (
            reset_telegram_write_confirmation_store_for_tests,
        )

        reset_telegram_write_confirmation_store_for_tests()
        bot = self.AsyncFakeBot()
        telebot = self._telebot_types()
        with patch(
            "app.bot.handlers.get",
            side_effect=lambda key, default="": "100" if key == "TG_CHAT_ID" else default,
        ), patch(
            "app.bot.handlers._maintenance_task_busy", return_value=False
        ), patch("app.bot.handlers._start_sync_gy", return_value=False):
            handlers._register_commands(bot, telebot)
            sync_handler = next(
                handler for filters, handler in bot.message_handlers
                if filters.get("commands") == ["sync_gy"]
            )
            confirmation_handler = next(
                handler for filters, handler in bot.callback_handlers
                if filters["func"](SimpleNamespace(data="tgc:opaque"))
            )
            sync_handler(SimpleNamespace(
                text="/sync_gy", chat=SimpleNamespace(id=100),
                from_user=SimpleNamespace(id=9), message_id=1,
            ))
            callback_data = bot.replies[0][2]["reply_markup"].buttons[0].callback_data
            callback_message = SimpleNamespace(chat=SimpleNamespace(id=100), message_id=11)
            confirmation_handler(SimpleNamespace(
                id="confirm", data=callback_data, from_user=SimpleNamespace(id=9),
                message=callback_message,
            ))

        self.assertIn("STRM 同步未启动", bot.edits[-1][2])
        self.assertIsNone(bot.edits[-1][3].get("reply_markup"))

    def test_rss_thread_start_failure_finishes_progress_and_closes_confirmation(self):
        from app.modules.telegram_write_confirmations import (
            reset_telegram_write_confirmation_store_for_tests,
        )

        reset_telegram_write_confirmation_store_for_tests()
        bot = self.AsyncFakeBot()
        telebot = self._telebot_types()
        subscription = {
            "id": 7, "name": "mikanani", "enabled": 1,
            "updated_at": "2026-08-20 12:00:00",
        }
        with patch(
            "app.bot.handlers.get",
            side_effect=lambda key, default="": "100" if key == "TG_CHAT_ID" else default,
        ), patch(
            "app.bot.handlers.threading.Thread", self.FailingThread
        ), patch("app.database.get_rss_subscription", return_value=subscription):
            handlers._register_commands(bot, telebot)
            refresh_handler = next(
                handler for filters, handler in bot.message_handlers
                if filters.get("commands") == ["rss_refresh"]
            )
            confirmation_handler = next(
                handler for filters, handler in bot.callback_handlers
                if filters["func"](SimpleNamespace(data="tgc:opaque"))
            )
            refresh_handler(SimpleNamespace(
                text="/rss_refresh 7", chat=SimpleNamespace(id=100),
                from_user=SimpleNamespace(id=9), message_id=1,
            ))
            callback_data = bot.replies[0][2]["reply_markup"].buttons[0].callback_data
            confirmation_handler(SimpleNamespace(
                id="confirm", data=callback_data, from_user=SimpleNamespace(id=9),
                message=SimpleNamespace(chat=SimpleNamespace(id=100), message_id=11),
            ))

        edit_texts = [row[2] for row in bot.edits]
        self.assertTrue(any("操作未启动" in text for text in edit_texts))
        self.assertIn("操作未启动", edit_texts[-1])
        self.assertIsNone(bot.edits[-1][3].get("reply_markup"))

    def test_consumed_confirmation_replay_marks_original_card_expired(self):
        from app.modules.telegram_write_confirmations import (
            reset_telegram_write_confirmation_store_for_tests,
        )

        reset_telegram_write_confirmation_store_for_tests()
        bot = self.AsyncFakeBot()
        telebot = self._telebot_types()
        with patch(
            "app.bot.handlers.get",
            side_effect=lambda key, default="": "100" if key == "TG_CHAT_ID" else default,
        ), patch(
            "app.bot.handlers._maintenance_task_busy", return_value=False
        ), patch("app.bot.handlers._start_sync_gy", return_value=True):
            handlers._register_commands(bot, telebot)
            sync_handler = next(
                handler for filters, handler in bot.message_handlers
                if filters.get("commands") == ["sync_gy"]
            )
            confirmation_handler = next(
                handler for filters, handler in bot.callback_handlers
                if filters["func"](SimpleNamespace(data="tgc:opaque"))
            )
            sync_handler(SimpleNamespace(
                text="/sync_gy", chat=SimpleNamespace(id=100),
                from_user=SimpleNamespace(id=9), message_id=1,
            ))
            callback_data = bot.replies[0][2]["reply_markup"].buttons[0].callback_data
            callback = SimpleNamespace(
                id="confirm", data=callback_data, from_user=SimpleNamespace(id=9),
                message=SimpleNamespace(chat=SimpleNamespace(id=100), message_id=11),
            )
            confirmation_handler(callback)
            confirmation_handler(callback)

        self.assertIn("确认已失效", bot.edits[-1][2])
        self.assertIsNone(bot.edits[-1][3].get("reply_markup"))
        self.assertTrue(bot.answers[-1][1].get("show_alert"))

    def test_organize_start_rechecks_current_configuration_before_locking(self):
        bot = self.FakeBot()
        telebot = self._telebot_types()
        message = SimpleNamespace(chat=SimpleNamespace(id=100), message_id=1)
        fake_lock = Mock()
        with patch(
            "app.bot.handlers._configured_organize_sources", return_value=[]
        ), patch(
            "app.bot.handlers.get", return_value=""
        ), patch("app.bot.handlers._task_lock", fake_lock):
            self.assertFalse(handlers._start_organize_gy(bot, telebot, message))
        fake_lock.acquire.assert_not_called()
        self.assertIn("未配置整理源目录或目标目录", bot.replies[-1][1])
