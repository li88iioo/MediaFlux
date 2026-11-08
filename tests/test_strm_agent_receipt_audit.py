"""STRM Agent 确认回执区分真实忙碌与关闭/线程启动失败。"""

from __future__ import annotations

from app.modules.process_lock import CrossProcessLock
from typing import Any, cast

import json
import threading
import unittest
from unittest.mock import patch

from app import database as db
from app.modules import scheduler as strm_runtime
from app.modules.scheduler import STRMScheduler
from tests.support import isolated_test_database


class STRMAgentReceiptAuditTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_test_database())
        self.scheduler = STRMScheduler()
        self.scheduler._run_lock = cast(CrossProcessLock, threading.Lock())
        self.enterContext(
            patch("app.modules.scheduler.get_scheduler", return_value=self.scheduler)
        )
        self.enterContext(
            patch.object(self.scheduler, "validate_config", return_value="")
        )
        self.enterContext(
            patch.object(self.scheduler, "status", return_value={"running": False})
        )
        self.config_value = "synthetic"
        original_get = strm_runtime.config.get
        self.enterContext(patch.object(strm_runtime.config, "get", side_effect=lambda key, default="":
            self.config_value if key in strm_runtime._STRM_CONFIRMATION_KEYS else original_get(key, default)))
        self.enterContext(patch.object(strm_runtime, "configured_strm_source_plans", return_value=([], "")))
        self.enterContext(patch("app.modules.scheduler._update_strm_requests"))
        self.enterContext(
            patch("app.modules.scheduler._publish_linked_notification_threads")
        )

    def test_stop_after_preview_is_not_reported_as_a_running_task(self):
        preview = strm_runtime.prepare_strm_run()
        fingerprint = preview.pop("fingerprint")
        self.assertTrue(preview["ok"])
        self.scheduler._stop_event.set()
        result = strm_runtime.submit_strm_run(expected_fingerprint=fingerprint)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")
        assert result["error"] is not None
        self.assertIn("停止", result["error"])
        self.assertNotIn("已在运行", result["summary"])
        self.assertIsNone(self.scheduler._worker)

    def test_thread_start_failure_has_failed_receipt_and_releases_admission(self):
        _preview = strm_runtime.prepare_strm_run()
        fingerprint = _preview.pop("fingerprint")
        with patch(
            "app.modules.scheduler.threading.Thread.start",
            side_effect=RuntimeError("synthetic thread failure"),
        ):
            result = strm_runtime.submit_strm_run(expected_fingerprint=fingerprint)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")
        assert result["error"] is not None
        self.assertIn("启动失败", result["error"])
        self.assertTrue(self.scheduler._run_lock.acquire(blocking=False))
        self.scheduler._run_lock.release()
        self.assertIsNone(self.scheduler._worker)
        self.assertFalse(self.scheduler._running)
        row = db.get_last_task_run("strm_sync")
        assert row is not None
        self.assertEqual(row["status"], "failed")

    def test_persistence_failure_rejects_before_start_and_releases_admission(self):
        with patch("app.modules.scheduler.db.add_task_run", side_effect=OSError("db unavailable")), patch(
            "app.modules.scheduler.threading.Thread.start"
        ) as start:
            result = self.scheduler.trigger("manual")
        self.assertFalse(result["ok"])
        start.assert_not_called()
        self.assertTrue(self.scheduler._run_lock.acquire(blocking=False))
        self.scheduler._run_lock.release()
        self.assertFalse(self.scheduler._running)

    def test_startup_failure_settles_run_even_when_secondary_cleanup_fails(self):
        for failure in ("request_admission", "request_cleanup", "notification_cleanup", "run_cleanup", "insert"):
            with (
                self.subTest(failure=failure),
                patch("app.modules.scheduler.threading.Thread") as worker,
                patch("app.modules.scheduler._update_strm_requests") as update,
                patch("app.modules.scheduler._publish_linked_notification_threads") as publish,
                patch("app.modules.scheduler.db.add_task_run", wraps=db.add_task_run) as add,
                patch("app.modules.scheduler.db.finish_task_run", wraps=db.finish_task_run) as finish,
            ):
                worker.return_value.start.side_effect = RuntimeError("original start failure")
                if failure in {"request_admission", "insert"}:
                    update.side_effect = OSError("request state unavailable")
                elif failure == "request_cleanup":
                    update.side_effect = [None, OSError("request cleanup unavailable")]
                elif failure == "notification_cleanup":
                    publish.side_effect = RuntimeError("notification unavailable")
                else:
                    finish.side_effect = OSError("run storage unavailable")
                if failure == "insert":
                    add.side_effect = OSError("run admission unavailable")

                with self.assertLogs("app.modules.scheduler", level="ERROR") as logs:
                    result = self.scheduler.trigger("manual")

                self.assertEqual(result, {"ok": False, "error": "STRM 同步任务启动失败"})
                self.assertIsNone(self.scheduler._worker)
                self.assertFalse(self.scheduler._running)
                self.assertEqual(self.scheduler._current_trigger, "")
                self.assertEqual(self.scheduler._run_options, {})
                self.assertTrue(self.scheduler._run_released_event.is_set())
                self.assertTrue(self.scheduler._run_lock.acquire(blocking=False))
                self.scheduler._run_lock.release()
                self.assertIn("启动 STRM 同步线程失败", "\n".join(logs.output))
                if failure in {"request_admission", "insert"}:
                    worker.assert_not_called()
                else:
                    worker.return_value.start.assert_called_once()
                    self.assertIn("original start failure", "\n".join(logs.output))
                if failure == "insert":
                    finish.assert_not_called()
                    continue
                finish.assert_called_once()
                row = db.get_last_task_run("strm_sync")
                assert row is not None
                if failure == "run_cleanup":
                    self.assertEqual(row["status"], "running")
                    self.assertIn("保存 STRM 启动失败状态异常", "\n".join(logs.output))
                    publish.assert_called_once()
                else:
                    self.assertEqual(row["status"], "failed")
                    self.assertTrue(row["finished_at"])
                    self.assertEqual(row["error"], "STRM 同步任务启动失败")
                    if failure != "notification_cleanup":
                        publish.assert_not_called()

    def test_actual_busy_admission_keeps_conflict_and_never_starts_a_worker(self):
        _preview = strm_runtime.prepare_strm_run()
        fingerprint = _preview.pop("fingerprint")
        self.scheduler._run_lock.acquire()
        try:
            result = strm_runtime.submit_strm_run(expected_fingerprint=fingerprint)
        finally:
            self.scheduler._run_lock.release()
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "conflict")
        self.assertIn("已在运行", result["summary"])
        self.assertIsNone(self.scheduler._worker)

    def test_scoped_success_and_stale_configuration_use_the_same_confirmed_path(self):
        sources = [{"id": "a", "name": "Alpha"}, {"id": "b", "name": "Beta"}]
        arguments = {"source_ids": ["b", "a"]}
        with (
            patch(
                "app.modules.scheduler.configured_strm_source_plans",
                return_value=(sources, ""),
            ),
            patch.object(
                self.scheduler, "trigger", return_value={"ok": True, "run_id": 7}
            ) as trigger,
        ):
            preview = strm_runtime.prepare_strm_run(**arguments)
            fingerprint = preview.pop("fingerprint")
            self.assertTrue(preview["ok"])
            self.assertEqual(preview["data"]["source_count"], 2)
            result = strm_runtime.submit_strm_run(**arguments, expected_fingerprint=fingerprint)
            self.assertTrue(result["ok"])
            self.assertEqual(result["data"]["run_id"], 7)
            self.assertEqual(result["status"], "accepted")
            trigger.assert_called_once_with("manual", selected_source_ids=["b", "a"])
            self.config_value = "changed"
            with self.assertRaises(ValueError) as error:
                strm_runtime.submit_strm_run(**arguments, expected_fingerprint=fingerprint)
            self.assertIn("已变化", str(error.exception))
            self.assertEqual(trigger.call_count, 1)

    def test_other_rejections_are_failed_not_busy(self):
        for reason in (
            "STRM 变化目标持久化失败，已取消同步",
            "STRM 同步模式无效",
            "synthetic future refusal",
        ):
            with (
                self.subTest(reason=reason),
                patch.object(
                    self.scheduler,
                    "trigger",
                    return_value={"ok": False, "error": reason},
                ),
            ):
                _preview = strm_runtime.prepare_strm_run()
                fingerprint = _preview.pop("fingerprint")
                result = strm_runtime.submit_strm_run(expected_fingerprint=fingerprint)
                self.assertFalse(result["ok"])
                self.assertEqual(result["status"], "failed")
                self.assertNotIn("已在运行", result["summary"])
                if reason.startswith("synthetic"):
                    assert result["error"] is not None
                    self.assertNotIn(reason, result["error"])
                else:
                    self.assertEqual(result["error"], reason)


    def test_public_source_ids_preview_is_complete_and_rejects_unknown_scope(self):
        sources = [{"id": str(index), "name": "Same name", "path": "/PRIVATE-ROOT", "namespace": "private"}
                   for index in range(21)]
        with patch.object(strm_runtime, "configured_strm_source_plans", return_value=(sources, "")), patch.object(self.scheduler, "trigger") as trigger:
            preview = strm_runtime.prepare_strm_run([item["id"] for item in sources])
            self.assertEqual(preview["data"]["source_count"], 21)
            self.assertEqual(preview["data"]["sources"], [{"id": str(index), "name": "Same name"} for index in range(21)])
            self.assertNotIn("PRIVATE", json.dumps(preview))
            self.assertFalse(strm_runtime.prepare_strm_run(["not-configured"])["ok"])
            for invalid in ("0", True, [False], [""], [" 0"]):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    strm_runtime.prepare_strm_run(cast(Any, invalid))
            trigger.assert_not_called()
        self.assertEqual(db.list_task_runs("strm_sync"), [])

    def test_public_original_run_id_never_uses_another_runs_result_or_progress(self):
        failed = db.add_task_run("strm_sync", "manual")
        db.finish_task_run(failed, "failed", result=json.dumps({"stats": {"generated": 2, "private_path": "/PRIVATE"}}), error="SECRET URL")
        active = db.add_task_run("strm_sync", "manual")
        wrong = db.add_task_run("other-task", "manual")
        raw = {"running": True, "current_run_id": failed,
               "progress": {"stage": "scan", "completed": 1, "total": 3, "percent": 33, "detail": "/PRIVATE"}}
        with patch.object(self.scheduler, "status", return_value=raw):
            old = strm_runtime.strm_status(failed)
            self.assertEqual(old["status"], "failed")
            self.assertEqual(old["data"]["task"]["run_id"], failed)
            self.assertEqual(old["data"]["task"]["stats"], {"generated": 2})
            self.assertEqual(strm_runtime.strm_status(active)["data"]["task"]["progress"], {})
            raw["current_run_id"] = active
            current = strm_runtime.strm_status(active)
            self.assertEqual(current["status"], "running")
            self.assertEqual(current["data"]["task"]["progress"], {"stage": "scan", "completed": 1, "total": 3, "percent": 33})
            for result in (old, current):
                self.assertNotIn("PRIVATE", json.dumps(result))
                self.assertNotIn("SECRET", json.dumps(result))
            for identity in (wrong, active + 100):
                self.assertEqual(strm_runtime.strm_status(identity)["status"], "unknown")
        for invalid in (0, -1, True, "1"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                strm_runtime.strm_status(cast(Any, invalid))

    def test_public_diagnose_uses_real_ledger_counts_and_keeps_raw_errors_private(self):
        db.record_strm_failure(source_id="source-a", source_name="private-name", file_id="private-file",
            parent_id="parent", filename="SECRET.mkv", action="generate", rel_dir="/PRIVATE", target_rel_path="/PRIVATE/a.strm", error="SECRET URL")
        run_id = db.add_task_run("strm_sync", "manual")
        db.finish_task_run(run_id, "partial", result=json.dumps({"stats": {"generated": 1}, "changes": ["/PRIVATE"]}), error="SECRET")
        result = strm_runtime.diagnose_strm()
        self.assertEqual(result["status"], "attention")
        self.assertEqual(result["data"]["failures"]["open"], 1)
        self.assertEqual(result["data"]["recent_runs"][0]["run_id"], run_id)
        self.assertEqual(result["data"]["recent_runs"][0]["status"], "partial")
        self.assertEqual(result["data"]["recent_limit"], 5)
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertNotIn("SECRET", json.dumps(result))
