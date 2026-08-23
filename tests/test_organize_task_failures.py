from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from time import monotonic, sleep, time
from unittest.mock import MagicMock, patch

from app.clients.guangya import GuangYaClient
from app.modules.organize import OrganizeRules, Organizer
from app.modules.organize_tasks import OrganizeTaskManager, _cleanup_manual_source_root


class _ImmediateThread:
    def __init__(self, *, target, args=(), **_kwargs):
        self.target = target
        self.args = args

    def start(self):
        self.target(*self.args)


class OrganizerCleanupSafetyTests(unittest.TestCase):
    def test_audit_failure_skips_empty_directory_cleanup(self):
        client = MagicMock()
        client.list_dir.return_value = []
        organizer = Organizer(client=client, scraper=MagicMock())
        rules = OrganizeRules(
            target_dir_id="target",
            clean_empty=True,
            link_strm=False,
            notify_enabled=False,
            library_notify=False,
        )

        def mark_audit_failure(_organizer, _plans, _rules, stats, *_args, **_kwargs):
            stats["moved"] = 1
            stats["audit_failures"] = 1

        with patch("app.modules.organize.execute_organize_plans", side_effect=mark_audit_failure), patch.object(
            organizer, "_clean_empty_dirs_report"
        ) as cleanup:
            _plans, stats = organizer.organize(
                "source", rules, dry_run=False, post_actions=False
            )

        cleanup.assert_not_called()
        self.assertEqual(stats["audit_failures"], 1)
        self.assertEqual(stats["empty_dir_cleanup_skipped"], 1)
        self.assertIn("保留目录以便恢复", stats["empty_dir_cleanup_reasons"][0])


class ManualSourceRootCleanupTests(unittest.TestCase):
    def test_manual_task_cleans_selected_temporary_empty_root(self):
        organizer = MagicMock()
        organizer._clean_empty_dirs_report.return_value = {
            "cleaned": 1, "delete_failures": 0
        }
        stats = {"moved": 1, "failed": 0, "scan_errors": []}
        rules = OrganizeRules(target_dir_id="archive", clean_empty=True)

        with patch("app.modules.organize_tasks.config.get", return_value="[]"):
            _cleanup_manual_source_root(
                organizer, "temporary", "临时目录", rules, stats,
                trigger_type="manual",
            )

        organizer._clean_empty_dirs_report.assert_called_once_with(
            [("temporary", 0, "", 0)],
            protected_source_ids={"0", "archive"},
        )
        self.assertEqual(stats["source_dir_cleaned"], 1)
        self.assertEqual(stats["empty_dirs_cleaned"], 1)

    def test_manual_task_preserves_configured_permanent_source_root(self):
        organizer = MagicMock()
        stats = {"moved": 1, "failed": 0, "scan_errors": []}
        rules = OrganizeRules(target_dir_id="archive", clean_empty=True)
        configured = json.dumps([{"id": "source", "name": "永久来源"}])

        with patch("app.modules.organize_tasks.config.get", return_value=configured):
            _cleanup_manual_source_root(
                organizer, "source", "永久来源", rules, stats,
                trigger_type="manual",
            )

        organizer._clean_empty_dirs_report.assert_not_called()
        self.assertEqual(stats["source_dir_cleanup_protected"], 1)
        self.assertIn("来源根目录按安全策略保留", stats["empty_dir_cleanup_reasons"][0])

    def test_manual_task_fails_closed_when_source_config_is_invalid(self):
        organizer = MagicMock()
        stats = {"moved": 1, "failed": 0, "scan_errors": []}
        rules = OrganizeRules(target_dir_id="archive", clean_empty=True)

        with patch("app.modules.organize_tasks.config.get", return_value="not-json"):
            _cleanup_manual_source_root(
                organizer, "temporary", "临时目录", rules, stats,
                trigger_type="manual",
            )

        organizer._clean_empty_dirs_report.assert_not_called()
        self.assertEqual(stats["source_dir_cleanup_skipped"], 1)
        self.assertIn("配置无法安全解析", stats["empty_dir_cleanup_reasons"][0])


class OrganizeTaskFailureTests(unittest.TestCase):
    def test_failure_is_persisted_and_sent_to_trigger_chat(self):
        manager = OrganizeTaskManager()
        organizer = MagicMock()
        organizer.organize.side_effect = sqlite3.ProgrammingError("sentinel binding failure")

        with patch("app.modules.organize_tasks.Organizer", return_value=organizer), patch(
            "app.modules.organize_tasks.threading.Thread", _ImmediateThread
        ), patch("app.modules.organize_tasks.db.add_task_run", return_value=77), patch(
            "app.modules.organize_tasks.db.finish_task_run"
        ) as finish, patch("app.notifier.send_event", return_value=True) as send_event, self.assertLogs(
            "app.modules.organize_tasks", level="ERROR"
        ) as captured:
            result = manager.start(
                [{"id": "source", "name": "源目录"}],
                OrganizeRules(target_dir_id="target"),
                trigger_type="telegram",
                chat_id="12345",
            )

        self.assertTrue(result["ok"])
        status = manager.task_status()
        self.assertEqual(status["status"], "failed")
        self.assertNotIn("chat_id", status)
        self.assertIn("部分文件", status["error"])
        self.assertTrue(any("sentinel binding failure" in line for line in captured.output))
        organizer.organize.assert_called_once()
        self.assertTrue(organizer.organize.call_args.kwargs["require_complete_scan"])
        finish.assert_called_once()
        self.assertEqual(finish.call_args.args[1], "failed")
        self.assertIn("ProgrammingError", finish.call_args.kwargs["error"])
        send_event.assert_called_once()
        self.assertEqual(send_event.call_args.kwargs["chat_id"], "12345")
        self.assertTrue(status["notification_sent"])

    def test_failure_keeps_progress_terminal_when_notification_delivery_fails(self):
        manager = OrganizeTaskManager()
        organizer = MagicMock()
        organizer.organize.side_effect = RuntimeError("notification failure sentinel")

        with patch("app.modules.organize_tasks.Organizer", return_value=organizer), patch(
            "app.modules.organize_tasks.threading.Thread", _ImmediateThread
        ), patch("app.modules.organize_tasks.db.add_task_run", return_value=78), patch(
            "app.modules.organize_tasks.db.finish_task_run"
        ), patch("app.notifier.send_event", return_value=False):
            result = manager.start(
                [{"id": "source", "name": "源目录"}],
                OrganizeRules(target_dir_id="target"),
                trigger_type="telegram",
                chat_id="12345",
            )

        self.assertTrue(result["ok"])
        status = manager.task_status()
        self.assertEqual(status["status"], "failed")
        self.assertFalse(status["notification_sent"])


    def test_multi_source_task_uses_one_task_summary_and_keeps_confirmations(self):
        manager = OrganizeTaskManager()
        organizer = MagicMock()
        confirmation = {
            "identity": "待确认剧集",
            "directory": "Season 01",
            "source_name": "来源二",
            "files": [{"file_id": "file-2", "name": "Show - 02.mkv"}],
            "candidates": [{"tmdb_id": "2", "title": "候选", "media_type": "tv"}],
        }
        organizer.organize.side_effect = [
            ([], {
                "total": 1, "moved": 1, "failed": 0,
                "media_items": [{"tmdb_id": "1", "media_type": "tv", "season": 1, "episode": 1}],
                "confirmation_groups": [],
            }),
            ([], {
                "total": 1, "moved": 0, "need_confirm": 1, "failed": 0,
                "media_items": [{"tmdb_id": "1", "media_type": "tv", "season": 1, "episode": 9}],
                "confirmation_groups": [confirmation],
            }),
        ]

        with patch("app.modules.organize_tasks.Organizer", return_value=organizer) as organizer_cls, patch(
            "app.modules.organize_tasks.threading.Thread", _ImmediateThread
        ), patch("app.modules.organize_tasks.db.add_task_run", return_value=81), patch(
            "app.modules.organize_tasks.db.finish_task_run"
        ):
            result = manager.start(
                [{"id": "s1", "name": "来源一"}, {"id": "s2", "name": "来源二"}],
                OrganizeRules(target_dir_id="target"),
                trigger_type="telegram",
                chat_id="100",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(manager.task_status()["status"], "completed")
        self.assertEqual(organizer.organize.call_count, 2)
        organizer_cls.trigger_post_actions.assert_called_once()
        self.assertFalse(organizer_cls.trigger_post_actions.call_args.kwargs["notify_result"])
        organizer_cls.notify_task_results.assert_called_once()
        aggregate = organizer_cls.notify_task_results.call_args.args[0]
        self.assertEqual(len(aggregate["media_items"]), 2)
        self.assertEqual(aggregate["confirmation_groups"], [confirmation])
        self.assertTrue(manager.task_status()["notification_sent"])
        self.assertFalse(organizer_cls.notify_directory_results.called)

    def test_default_organizer_client_is_pinned_for_background_worker(self):
        manager = OrganizeTaskManager()
        validator = MagicMock()
        client = MagicMock()
        client.logged_in = True
        client.credential_generation = 3
        validator.client = client

        class _SwitchDefaultCredentialThread(_ImmediateThread):
            def start(self):
                client.credential_generation = 4
                self.target(*self.args)

        with patch("app.modules.organize_tasks.Organizer", return_value=validator), patch(
            "app.modules.organize_tasks.threading.Thread", _SwitchDefaultCredentialThread
        ), patch("app.modules.organize_tasks.db.add_task_run", return_value=80), patch(
            "app.modules.organize_tasks.db.finish_task_run"
        ) as finish, patch("app.notifier.send_event", return_value=True):
            result = manager.start(
                [{"id": "source", "name": "源目录"}],
                OrganizeRules(target_dir_id="target"),
            )

        self.assertTrue(result["ok"])
        self.assertEqual(manager.task_status()["status"], "failed")
        validator.organize.assert_not_called()
        finish.assert_called_once()
        self.assertEqual(finish.call_args.args[1], "failed")
        self.assertIn("RuntimeError", finish.call_args.kwargs["error"])

    def test_confirmed_client_generation_change_blocks_background_writes(self):
        manager = OrganizeTaskManager()
        organizer = MagicMock()
        client = MagicMock()
        client.logged_in = True
        client.credential_generation = 7

        class _SwitchCredentialThread(_ImmediateThread):
            def start(self):
                client.credential_generation = 8
                self.target(*self.args)

        with patch("app.modules.organize_tasks.Organizer", return_value=organizer), patch(
            "app.modules.organize_tasks.threading.Thread", _SwitchCredentialThread
        ), patch("app.modules.organize_tasks.db.add_task_run", return_value=79), patch(
            "app.modules.organize_tasks.db.finish_task_run"
        ) as finish, patch("app.notifier.send_event", return_value=True):
            result = manager.start(
                [{"id": "source", "name": "源目录"}],
                OrganizeRules(target_dir_id="target"),
                client=client,
                expected_credential_generation=7,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(manager.task_status()["status"], "failed")
        organizer.organize.assert_not_called()
        finish.assert_called_once()
        self.assertEqual(finish.call_args.args[1], "failed")
        self.assertIn("RuntimeError", finish.call_args.kwargs["error"])

    def test_same_client_token_refresh_does_not_invalidate_multi_source_task(self):
        class _RotatingRawClient:
            def __init__(self, access_token=None, refresh_token=None, device_id=None):
                self.token = access_token or ""
                self.refresh_token_value = refresh_token or ""
                self.device_id = device_id or "device"
                self.token_expires_at = time() + 1

            def refresh_token(self, _refresh_token=None):
                self.token = "rotated-access"
                self.refresh_token_value = "rotated-refresh"
                self.token_expires_at = time() + 3600
                return {
                    "access_token": self.token,
                    "refresh_token": self.refresh_token_value,
                    "expires_in": 3600,
                }

        class _Organizer:
            def __init__(self, client):
                self.client = client
                self.organized: list[str] = []

            def _validate_target_outside_source(self, _source_id, _target_id):
                _ = self.client.raw

            def organize(self, source_id, _rules, **_kwargs):
                self.organized.append(source_id)
                return [], {
                    "total": 0,
                    "moved": 0,
                    "failed": 0,
                    "scan_errors": [],
                    "replacement_cleanup_failed": 0,
                    "directories": {},
                }

        with tempfile.TemporaryDirectory() as root:
            token_file = Path(root) / "guangya_token.json"
            token_file.write_text(
                json.dumps({
                    "access_token": "old-access",
                    "refresh_token": "old-refresh",
                    "device_id": "device",
                    "expires_at": time() + 1,
                }),
                encoding="utf-8",
            )
            with patch("app.clients.guangya._load_raw", return_value=_RotatingRawClient):
                client = GuangYaClient(token_file=token_file)
                generation = client.credential_generation
                organizer = _Organizer(client)
                manager = OrganizeTaskManager()
                with patch(
                    "app.modules.organize_tasks.Organizer", return_value=organizer
                ), patch(
                    "app.modules.organize_tasks.threading.Thread", _ImmediateThread
                ), patch(
                    "app.modules.organize_tasks.db.add_task_run", return_value=81
                ), patch(
                    "app.modules.organize_tasks.db.finish_task_run"
                ), patch(
                    "app.notifier.send_event", return_value=True
                ):
                    result = manager.start(
                        [
                            {"id": "source-a", "name": "来源 A"},
                            {"id": "source-b", "name": "来源 B"},
                        ],
                        OrganizeRules(target_dir_id="target"),
                        client=client,
                    )

        self.assertTrue(result["ok"])
        self.assertEqual(client.credential_generation, generation)
        self.assertEqual(manager.task_status()["status"], "completed")
        self.assertEqual(organizer.organized, ["source-a", "source-b"])

    def test_multi_source_run_protects_every_configured_source_root(self):
        manager = OrganizeTaskManager()
        organizer = MagicMock()
        organizer.organize.return_value = ([], {
            "total": 0, "moved": 0, "failed": 0, "scan_errors": [],
            "replacement_cleanup_failed": 0, "directories": {},
        })
        sources = [
            {"id": "source-a", "name": "来源 A"},
            {"id": "source-b", "name": "来源 B"},
        ]

        with patch("app.modules.organize_tasks.Organizer", return_value=organizer), patch(
            "app.modules.organize_tasks.threading.Thread", _ImmediateThread
        ), patch("app.modules.organize_tasks.db.add_task_run", return_value=78), patch(
            "app.modules.organize_tasks.db.finish_task_run"
        ):
            result = manager.start(
                sources,
                OrganizeRules(target_dir_id="target", clean_empty=True),
            )

        self.assertTrue(result["ok"])
        self.assertEqual(organizer.organize.call_count, 2)
        for call in organizer.organize.call_args_list:
            self.assertEqual(
                call.kwargs["protected_source_ids"],
                {"source-a", "source-b"},
            )

    def test_shutdown_rejects_new_tasks(self):
        manager = OrganizeTaskManager()
        self.assertTrue(manager.shutdown(timeout=0))
        result = manager.start(
            [{"id": "source", "name": "源目录"}],
            OrganizeRules(target_dir_id="target"),
        )
        self.assertFalse(result["ok"])
        self.assertIn("服务正在停止", result["error"])

    def test_per_file_failure_persists_partial_state(self):
        manager = OrganizeTaskManager()
        organizer = MagicMock()
        organizer.organize.return_value = ([], {
            "total": 2, "moved": 1, "failed": 1, "scan_errors": [],
            "replacement_cleanup_failed": 0, "directories": {},
        })

        with patch("app.modules.organize_tasks.Organizer", return_value=organizer), patch(
            "app.modules.organize_tasks.threading.Thread", _ImmediateThread
        ), patch("app.modules.organize_tasks.db.add_task_run", return_value=88), patch(
            "app.modules.organize_tasks.db.finish_task_run"
        ) as finish:
            result = manager.start(
                [{"id": "source", "name": "源目录"}],
                OrganizeRules(target_dir_id="target", link_strm=True),
            )

        self.assertTrue(result["ok"])
        self.assertEqual(manager.task_status()["status"], "partial")
        finish.assert_called_once()
        self.assertEqual(finish.call_args.args[1], "partial")


    def test_audit_failure_persists_partial_state(self):
        manager = OrganizeTaskManager()
        organizer = MagicMock()
        organizer.organize.return_value = ([], {
            "total": 1, "moved": 1, "failed": 0, "scan_errors": [],
            "replacement_cleanup_failed": 0, "audit_failures": 1,
            "directories": {},
        })

        with patch("app.modules.organize_tasks.Organizer", return_value=organizer), patch(
            "app.modules.organize_tasks.threading.Thread", _ImmediateThread
        ), patch("app.modules.organize_tasks.db.add_task_run", return_value=90), patch(
            "app.modules.organize_tasks.db.finish_task_run"
        ) as finish:
            result = manager.start(
                [{"id": "source", "name": "源目录"}],
                OrganizeRules(target_dir_id="target", link_strm=True),
            )

        self.assertTrue(result["ok"])
        self.assertEqual(manager.task_status()["status"], "partial")
        finish.assert_called_once()
        self.assertEqual(finish.call_args.args[1], "partial")

    def test_empty_directory_cleanup_failure_persists_partial_state(self):
        manager = OrganizeTaskManager()
        organizer = MagicMock()
        organizer.organize.return_value = ([], {
            "total": 0, "moved": 0, "failed": 0, "scan_errors": [],
            "replacement_cleanup_failed": 0,
            "empty_dir_cleanup_failed": 1,
            "directories": {},
        })

        with patch("app.modules.organize_tasks.Organizer", return_value=organizer), patch(
            "app.modules.organize_tasks.threading.Thread", _ImmediateThread
        ), patch("app.modules.organize_tasks.db.add_task_run", return_value=89), patch(
            "app.modules.organize_tasks.db.finish_task_run"
        ) as finish:
            result = manager.start(
                [{"id": "source", "name": "源目录"}],
                OrganizeRules(target_dir_id="target", clean_empty=True),
            )

        self.assertTrue(result["ok"])
        self.assertEqual(manager.task_status()["status"], "partial")
        finish.assert_called_once()
        self.assertEqual(finish.call_args.args[1], "partial")

    def test_manual_operations_queue_fifo_and_keep_terminal_history(self):
        manager = OrganizeTaskManager()
        manager._lock = threading.Lock()
        first_started = threading.Event()
        release_first = threading.Event()
        second_finished = threading.Event()
        order: list[str] = []

        def first_callback():
            order.append("first")
            first_started.set()
            self.assertTrue(release_first.wait(timeout=2))
            return {"stats": {"moved": 1}}

        def second_callback():
            order.append("second")
            second_finished.set()
            return {"stats": {"moved": 1}}

        first = manager.start_operation(
            "目录刮削",
            "first",
            first_callback,
            queue_if_busy=True,
            dedupe_key="manual:first",
        )
        self.assertTrue(first["ok"])
        self.assertTrue(first_started.wait(timeout=2))

        second = manager.start_operation(
            "目录刮削",
            "second",
            second_callback,
            queue_if_busy=True,
            dedupe_key="manual:second",
        )
        self.assertTrue(second["ok"])
        self.assertTrue(second["queued"])
        self.assertEqual(second["queue_position"], 1)
        queued = manager.task_status()["operation_queue"]["items"]
        self.assertEqual([item["id"] for item in queued], [second["task_id"]])

        replayed = manager.start_operation(
            "目录刮削",
            "second",
            second_callback,
            queue_if_busy=True,
            dedupe_key="manual:second",
        )
        self.assertTrue(replayed["replayed"])
        self.assertEqual(replayed["task_id"], second["task_id"])

        release_first.set()
        self.assertTrue(second_finished.wait(timeout=3))
        deadline = monotonic() + 3
        history = manager.task_status()["operation_history"]
        while len(history) < 2 and monotonic() < deadline:
            sleep(0.01)
            history = manager.task_status()["operation_history"]

        self.assertEqual(order, ["first", "second"])
        self.assertEqual(
            [item["id"] for item in history[:2]],
            [second["task_id"], first["task_id"]],
        )
        self.assertTrue(all(item["status"] == "completed" for item in history[:2]))
        manager.begin_shutdown()

    def test_manual_operation_queued_behind_full_organize_starts_after_release(self):
        manager = OrganizeTaskManager()
        manager._lock = threading.Lock()
        organize_started = threading.Event()
        release_organize = threading.Event()
        operation_finished = threading.Event()
        organizer = MagicMock()
        organizer._validate_target_outside_source.return_value = None

        def organize(*_args, **_kwargs):
            organize_started.set()
            self.assertTrue(release_organize.wait(timeout=3))
            return [], {"total": 1, "moved": 1, "failed": 0}

        organizer.organize.side_effect = organize

        with patch("app.modules.organize_tasks.Organizer", return_value=organizer), patch(
            "app.modules.organize_tasks.db.add_task_run", return_value=0
        ), patch.object(OrganizeTaskManager, "_wake_download_tracker"):
            full = manager.start(
                [{"id": "source", "name": "源目录"}],
                OrganizeRules(target_dir_id="target", notify_enabled=False, library_notify=False),
            )
            self.assertTrue(full["ok"])
            self.assertTrue(organize_started.wait(timeout=2))

            queued = manager.start_operation(
                "目录刮削",
                "manual",
                lambda: operation_finished.set() or {"stats": {"moved": 1}},
                queue_if_busy=True,
                dedupe_key="manual:after-full",
            )
            self.assertTrue(queued["ok"])
            self.assertTrue(queued["queued"])

            release_organize.set()
            self.assertTrue(operation_finished.wait(timeout=4))
            deadline = monotonic() + 3
            while manager.task_result(queued["task_id"])["status"] != "completed" and monotonic() < deadline:
                sleep(0.01)

        self.assertEqual(manager.task_result(full["task_id"])["status"], "completed")
        self.assertEqual(manager.task_result(queued["task_id"])["status"], "completed")
        manager.begin_shutdown()

    def test_manual_operation_queue_rolls_back_when_dispatcher_start_fails(self):
        manager = OrganizeTaskManager()
        manager._lock = threading.Lock()
        manager._lock.acquire()
        dispatcher = MagicMock()
        dispatcher.is_alive.return_value = False
        dispatcher.start.side_effect = RuntimeError("thread unavailable")
        try:
            with patch(
                "app.modules.organize_tasks.threading.Thread",
                return_value=dispatcher,
            ):
                result = manager.start_operation(
                    "目录刮削",
                    "queue-start-failure",
                    lambda: {"stats": {"moved": 1}},
                    queue_if_busy=True,
                    dedupe_key="manual:queue-start-failure",
                )
        finally:
            manager._lock.release()

        self.assertFalse(result["ok"])
        self.assertTrue(result["retryable"])
        self.assertEqual(result["error_code"], "queue_dispatcher_start_failed")
        self.assertEqual(manager.task_status()["operation_queue"]["items"], [])
        self.assertIsNone(manager._operation_dispatcher)

    def test_shutdown_records_queued_manual_operation_as_stopped(self):
        manager = OrganizeTaskManager()
        manager._lock = threading.Lock()
        manager._lock.acquire()
        queued = manager.start_operation(
            "目录刮削",
            "queued-on-shutdown",
            lambda: {"stats": {"moved": 1}},
            queue_if_busy=True,
            dedupe_key="manual:shutdown",
        )
        self.assertTrue(queued["queued"])

        manager.begin_shutdown()
        manager._lock.release()

        terminal = manager.task_result(queued["task_id"])
        self.assertIsNotNone(terminal)
        self.assertEqual(terminal["status"], "stopped")
        self.assertIn("服务关闭", terminal["error"])
        history = manager.task_status()["operation_history"]
        self.assertEqual(history[0]["id"], queued["task_id"])
        self.assertEqual(history[0]["status"], "stopped")

    def test_single_operation_cleanup_failure_is_reported_as_partial(self):
        manager = OrganizeTaskManager()

        with patch("app.modules.organize_tasks.threading.Thread", _ImmediateThread):
            result = manager.start_operation(
                "目录刮削",
                "source",
                lambda: {
                    "stats": {
                        "failed": 0,
                        "scan_errors": [],
                        "source_dir_cleanup_failed": 1,
                    }
                },
            )

        self.assertTrue(result["ok"])
        status = manager.task_status()
        self.assertEqual(status["status"], "partial")
        self.assertEqual(status["message"], "目录刮削部分完成")
        self.assertEqual(status["result"]["stats"]["source_dir_cleanup_failed"], 1)


if __name__ == "__main__":
    unittest.main()
