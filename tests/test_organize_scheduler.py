"""Task 9：网盘整理 Cron 调度器隔离测试。"""
from __future__ import annotations

import json
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

try:
    from app.modules.organize_scheduler import OrganizeScheduler
except ModuleNotFoundError:
    OrganizeScheduler = None

from app.modules.organize import OrganizeRules, Organizer
from app.modules.organize_tasks import OrganizeTaskManager


class FakeManager:
    def __init__(self, start_result: dict | None = None) -> None:
        self.calls: list[tuple[list[dict[str, str]], OrganizeRules, str]] = []
        self.start_result = start_result or {
            "ok": True,
            "task_id": "cron-task",
            "message": "整理任务已启动",
        }
        self.current = {
            "id": "",
            "status": "idle",
            "message": "暂无整理任务",
            "stats": {},
            "error": "",
            "finished_at": "",
            "trigger_type": "",
        }

    def start(self, sources, rules, *, trigger_type="manual"):
        copied = [dict(source) for source in sources]
        self.calls.append((copied, rules, trigger_type))
        result = dict(self.start_result)
        if result.get("ok"):
            self.current = {
                "id": result["task_id"],
                "status": "running",
                "message": result.get("message", "整理任务已启动"),
                "stats": {},
                "error": "",
                "finished_at": "",
                "trigger_type": trigger_type,
            }
        return result

    def task_status(self):
        return dict(self.current)


class SchedulerHarness:
    def __init__(self, *, enabled=True, cron="0 4 * * *", sources=None, target="target-1", now=None, manager=None):
        self.values = {
            "GY_ORGANIZE_SCHEDULE_ENABLED": "1" if enabled else "0",
            "GY_ORGANIZE_SCHEDULE_CRON": cron,
            "GY_ORGANIZE_SOURCE_DIRS": json.dumps(
                sources if sources is not None else [{"id": "source-1", "name": "源一"}],
                ensure_ascii=False,
            ),
            "GY_ORGANIZE_TARGET_DIR": target,
        }
        self.manager = manager or FakeManager()
        self.now = now or datetime(2026, 7, 25, 3, 0, 0)

    def get_value(self, key, default=""):
        return self.values.get(key, default)

    def get_flag(self, key, default=False):
        value = self.values.get(key)
        if value is None:
            return default
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def build(self, *, interval=60.0):
        if OrganizeScheduler is None:
            return None
        return OrganizeScheduler(
            manager=self.manager,
            get_value=self.get_value,
            get_flag=self.get_flag,
            now=lambda: self.now,
            check_interval=interval,
        )


class OrganizeSchedulerTests(unittest.TestCase):
    def require_scheduler(self):
        self.assertIsNotNone(OrganizeScheduler, "缺少 app.modules.organize_scheduler")
        return OrganizeScheduler

    def test_validate_cron_accepts_standard_five_fields(self):
        scheduler_class = self.require_scheduler()
        self.assertTrue(scheduler_class.validate_cron("*/15 1-5 * * 1-5"))

    def test_validate_cron_rejects_six_fields(self):
        scheduler_class = self.require_scheduler()
        self.assertFalse(scheduler_class.validate_cron("0 */15 1-5 * * 1-5"))

    def test_disabled_schedule_clears_next_run_without_starting_task(self):
        self.require_scheduler()
        scheduler = SchedulerHarness(enabled=False).build()
        self.assertIsNotNone(scheduler)
        scheduler._next_run = datetime(2026, 7, 25, 3, 1, 0)

        scheduler._tick()

        status = scheduler.status()
        self.assertFalse(status["enabled"])
        self.assertEqual(status["next_run"], "")
        self.assertEqual(scheduler._manager.calls, [])

    def test_overlap_is_recorded_as_skipped(self):
        self.require_scheduler()
        manager = FakeManager({"ok": False, "error": "网盘整理任务正在运行"})
        harness = SchedulerHarness(cron="* * * * *", manager=manager)
        scheduler = harness.build()
        scheduler._loaded_cron = "* * * * *"
        scheduler._next_run = datetime(2026, 7, 25, 2, 59, 0)

        scheduler._tick()

        last = scheduler.status()["last_result"]
        self.assertEqual(last["outcome"], "skipped")
        self.assertEqual(last["trigger_type"], "cron")
        self.assertIn("正在运行", last["message"])

    def test_due_run_starts_one_task_with_all_sources_and_cron_trigger(self):
        self.require_scheduler()
        sources = [
            {"id": "source-1", "name": "源一"},
            {"id": "source-2", "name": "源二"},
        ]
        harness = SchedulerHarness(cron="* * * * *", sources=sources)
        scheduler = harness.build()
        scheduler._loaded_cron = "* * * * *"
        scheduler._next_run = datetime(2026, 7, 25, 2, 59, 0)

        scheduler._tick()

        self.assertEqual(len(harness.manager.calls), 1)
        called_sources, rules, trigger_type = harness.manager.calls[0]
        self.assertEqual(called_sources, sources)
        self.assertEqual(rules.target_dir_id, "target-1")
        self.assertEqual(trigger_type, "cron")
        last = scheduler.status()["last_result"]
        self.assertEqual(last["source_count"], 2)
        self.assertEqual(last["outcome"], "started")

    def test_completed_cron_task_updates_last_result_with_aggregate_stats(self):
        self.require_scheduler()
        harness = SchedulerHarness(cron="0 4 * * *")
        scheduler = harness.build()
        scheduler._loaded_cron = "0 4 * * *"
        scheduler._next_run = datetime(2026, 7, 25, 2, 59, 0)
        scheduler._tick()
        harness.manager.current.update({
            "status": "completed",
            "message": "整理任务已完成",
            "stats": {"moved": 7, "skipped": 3, "failed": 1},
            "finished_at": "2026-07-25 03:12:00",
        })

        last = scheduler.status()["last_result"]

        self.assertEqual(last["outcome"], "completed")
        self.assertEqual(last["stats"], {"moved": 7, "skipped": 3, "failed": 1})
        self.assertEqual(last["trigger_type"], "cron")

    def test_partial_cron_task_updates_last_result_and_releases_active_task(self):
        self.require_scheduler()
        harness = SchedulerHarness(cron="0 4 * * *")
        scheduler = harness.build()
        scheduler._loaded_cron = "0 4 * * *"
        scheduler._next_run = datetime(2026, 7, 25, 2, 59, 0)
        scheduler._tick()
        harness.manager.current.update({
            "status": "partial",
            "message": "整理任务部分完成",
            "stats": {"moved": 7, "audit_failures": 1},
            "finished_at": "2026-07-25 03:12:00",
        })

        last = scheduler.status()["last_result"]

        self.assertEqual(last["outcome"], "partial")
        self.assertEqual(last["stats"], {"moved": 7, "audit_failures": 1})
        self.assertEqual(scheduler._active_task_id, "")

    def test_completed_cron_task_is_resolved_from_history_after_current_task_changes(self):
        self.require_scheduler()
        manager = FakeManager()
        scheduler = SchedulerHarness(cron="0 4 * * *", manager=manager).build()
        scheduler._active_task_id = "cron-task"
        scheduler._last_result = {
            "task_id": "cron-task",
            "trigger_type": "cron",
            "outcome": "started",
            "message": "整理任务已启动",
            "started_at": "2026-07-25 03:00:00",
            "finished_at": "",
            "source_count": 1,
            "stats": {},
        }
        manager.current = {
            "id": "manual-task",
            "status": "running",
            "message": "目录刮削已启动",
            "stats": {},
            "error": "",
            "finished_at": "",
            "trigger_type": "manual",
        }
        manager.task_result = MagicMock(return_value={
            "id": "cron-task",
            "status": "completed",
            "message": "整理任务已完成",
            "stats": {"moved": 4},
            "error": "",
            "finished_at": "2026-07-25 03:10:00",
        })

        last = scheduler.status()["last_result"]

        manager.task_result.assert_called_once_with("cron-task")
        self.assertEqual(last["outcome"], "completed")
        self.assertEqual(last["stats"], {"moved": 4})
        self.assertEqual(scheduler._active_task_id, "")

    def test_reload_wakes_service_and_start_keeps_single_daemon_thread(self):
        self.require_scheduler()
        harness = SchedulerHarness(enabled=False)
        scheduler = harness.build(interval=60.0)
        scheduler.reload()
        self.assertTrue(scheduler._wake_event.is_set())

        scheduler.start()
        first_thread = scheduler._thread
        scheduler.start()
        try:
            self.assertIs(first_thread, scheduler._thread)
            self.assertTrue(first_thread.daemon)
            self.assertTrue(first_thread.is_alive())
        finally:
            scheduler.stop()
        self.assertFalse(first_thread.is_alive())

    def test_stop_does_not_forget_a_scheduler_thread_that_is_still_alive(self):
        self.require_scheduler()
        scheduler = SchedulerHarness(enabled=False).build(interval=0.05)

        class StubbornThread:
            daemon = True

            def is_alive(self):
                return True

            def join(self, timeout=None):
                self.timeout = timeout

        thread = StubbornThread()
        scheduler._thread = thread

        scheduler.stop()

        self.assertIs(scheduler._thread, thread)

    def test_task_status_scheduler_callback_error_is_opaque_in_logs_and_payload(self):
        manager = OrganizeTaskManager()
        scheduler = MagicMock()
        scheduler.status.side_effect = RuntimeError("opaque-provider-secret")

        with patch(
            "app.modules.organize_scheduler.get_organize_scheduler",
            return_value=scheduler,
        ), self.assertLogs("app.modules.organize_tasks", level="WARNING") as captured:
            status = manager.status()

        serialized = json.dumps(status, ensure_ascii=False) + "\n" + "\n".join(captured.output)
        self.assertNotIn("opaque-provider-secret", serialized)
        self.assertEqual(status["schedule"]["config_error"], "调度状态不可用")
        self.assertIn("RuntimeError", serialized)

    def test_task_status_endpoint_includes_scheduler_snapshot(self):
        manager = OrganizeTaskManager()
        scheduler = MagicMock()
        scheduler.status.return_value = {
            "enabled": True,
            "cron": "0 4 * * *",
            "cron_valid": True,
            "config_error": "",
            "next_run": "2026-07-26 04:00:00",
            "last_result": {"outcome": "completed"},
        }

        with patch(
            "app.modules.organize_scheduler.get_organize_scheduler",
            return_value=scheduler,
        ):
            status = manager.status()

        self.assertIn("schedule", status)
        self.assertEqual(status["schedule"]["next_run"], "2026-07-26 04:00:00")
        self.assertEqual(status["schedule"]["last_result"]["outcome"], "completed")

    def test_application_lifecycle_starts_and_stops_organize_scheduler(self):
        from app import main

        strm = MagicMock()
        rss = MagicMock()
        organize = MagicMock()
        downloads = MagicMock()
        verification = MagicMock()
        patrol = MagicMock()
        jobs = MagicMock()
        local_media = MagicMock()
        with patch("app.modules.scheduler.get_scheduler", return_value=strm), patch(
            "app.modules.rss_scheduler.get_rss_scheduler", return_value=rss
        ), patch(
            "app.modules.organize_scheduler.get_organize_scheduler", return_value=organize
        ), patch(
            "app.modules.local_media_scheduler.get_local_media_scheduler", return_value=local_media
        ), patch(
            "app.modules.download_tracker.get_download_tracker", return_value=downloads
        ), patch(
            "app.modules.agent_download_verification_scheduler.get_download_library_verification_scheduler",
            return_value=verification,
        ), patch(
            "app.modules.agent_library_patrol_scheduler.get_agent_library_patrol_scheduler",
            return_value=patrol,
        ), patch(
            "app.modules.agent_jobs_scheduler.get_agent_jobs_scheduler",
            return_value=jobs,
        ), patch.object(main.config, "get", return_value=""), patch(
            "app.agent.feature_gate.is_agent_enabled", return_value=True
        ), patch(
            "app.modules.organize_confirmations.start_confirmation_dispatcher"
        ) as confirmation_start, patch(
            "app.modules.organize_confirmations.stop_confirmation_dispatcher"
        ) as confirmation_stop, patch("app.bot.stop_bot"):
            main.start_background_services()
            main.stop_background_services()

        organize.start.assert_called_once_with()
        organize.stop.assert_called_once_with()
        local_media.start.assert_called_once_with()
        local_media.stop.assert_called_once_with()
        verification.start.assert_called_once_with()
        verification.stop.assert_called_once_with()
        patrol.start.assert_called_once_with()
        patrol.stop.assert_called_once_with()
        jobs.start.assert_called_once_with()
        jobs.stop.assert_called_once_with()
        confirmation_start.assert_called_once_with()
        confirmation_stop.assert_called_once_with()

    def test_application_shutdown_reports_undrained_organize_worker(self):
        from app import main

        manager = MagicMock()
        manager.shutdown.return_value = False
        subscriptions = MagicMock()
        subscriptions.stop.return_value = True
        with patch("app.modules.organize_tasks.get_organize_manager", return_value=manager), patch(
            "app.bot.stop_bot"
        ), patch(
            "app.modules.agent_jobs_scheduler.get_agent_jobs_scheduler"
        ), patch(
            "app.modules.agent_library_patrol_scheduler.get_agent_library_patrol_scheduler"
        ), patch(
            "app.modules.agent_download_verification_scheduler.get_download_library_verification_scheduler"
        ), patch(
            "app.modules.download_tracker.get_download_tracker"
        ), patch(
            "app.modules.rss_scheduler.get_rss_scheduler"
        ), patch(
            "app.modules.media_subscription_scheduler.get_media_subscription_scheduler",
            return_value=subscriptions,
        ), patch(
            "app.modules.organize_scheduler.get_organize_scheduler"
        ), patch(
            "app.modules.local_media_scheduler.get_local_media_scheduler"
        ), patch(
            "app.modules.organize_confirmations.stop_confirmation_dispatcher"
        ), patch(
            "app.modules.scheduler.get_scheduler"
        ), self.assertLogs("app.main", level="WARNING") as captured:
            self.assertFalse(main.stop_background_services())

        manager.begin_shutdown.assert_called_once_with()
        manager.shutdown.assert_called_once_with(timeout=30.0)
        self.assertIn("停止网盘整理任务超时", "\n".join(captured.output))

    def test_verification_scheduler_stop_error_logs_only_exception_type(self):
        from app import main

        verification = MagicMock()
        verification.stop.side_effect = RuntimeError("SECRET-TOKEN")
        with patch("app.bot.stop_bot"), patch(
            "app.modules.agent_jobs_scheduler.get_agent_jobs_scheduler"
        ), patch(
            "app.modules.agent_library_patrol_scheduler.get_agent_library_patrol_scheduler"
        ), patch(
            "app.modules.agent_download_verification_scheduler.get_download_library_verification_scheduler",
            return_value=verification,
        ), patch("app.modules.download_tracker.get_download_tracker"), patch(
            "app.modules.rss_scheduler.get_rss_scheduler"
        ), patch("app.modules.organize_scheduler.get_organize_scheduler"), patch(
            "app.modules.local_media_scheduler.get_local_media_scheduler"
        ), patch("app.modules.scheduler.get_scheduler"), self.assertLogs(
            "app.main", level="WARNING"
        ) as captured:
            main.stop_background_services()

        output = "\n".join(captured.output)
        self.assertIn("RuntimeError", output)
        self.assertNotIn("SECRET-TOKEN", output)

    def test_task_manager_exposes_cron_trigger_type(self):
        import inspect

        self.assertIn("trigger_type", inspect.signature(OrganizeTaskManager.start).parameters)
        manager = OrganizeTaskManager()

        class DeferredThread:
            daemon = True

            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                pass

        with patch("app.modules.organize_tasks.threading.Thread", DeferredThread), patch.object(
            Organizer, "_validate_target_outside_source"
        ):
            result = manager.start(
                [{"id": "source-1", "name": "源一"}],
                OrganizeRules(target_dir_id="target-1"),
                trigger_type="cron",
            )
        try:
            self.assertTrue(result["ok"])
            self.assertEqual(manager.task_status()["trigger_type"], "cron")
        finally:
            manager._lock.release()


if __name__ == "__main__":
    unittest.main()
