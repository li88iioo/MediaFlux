"""STRM Agent 确认回执区分真实忙碌与关闭/线程启动失败。"""

from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

from app.agent.domain_catalog import strm_runtime
from app.agent.errors import AgentToolError
from app.modules.scheduler import STRMScheduler
from tests.support import isolated_test_database


class STRMAgentReceiptAuditTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_test_database())
        self.scheduler = STRMScheduler()
        self.scheduler._run_lock = threading.Lock()
        self.enterContext(
            patch("app.modules.scheduler.get_scheduler", return_value=self.scheduler)
        )
        self.enterContext(
            patch.object(self.scheduler, "validate_config", return_value="")
        )
        self.enterContext(
            patch.object(self.scheduler, "status", return_value={"running": False})
        )
        self.config = self.enterContext(
            patch.object(strm_runtime.config, "get", return_value="synthetic")
        )
        self.enterContext(patch("app.modules.scheduler._update_strm_requests"))
        self.enterContext(
            patch("app.modules.scheduler._publish_linked_notification_threads")
        )

    def test_stop_after_preview_is_not_reported_as_a_running_task(self):
        preview, fingerprint = strm_runtime.prepare_strm_run_once({})
        self.assertTrue(preview.ok)
        self.scheduler._stop_event.set()
        result = strm_runtime.run_strm_once_confirmed({}, fingerprint)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "failed")
        self.assertIn("停止", result.error)
        self.assertNotIn("已在运行", result.summary)
        self.assertIsNone(self.scheduler._worker)

    def test_thread_start_failure_has_failed_receipt_and_releases_admission(self):
        _preview, fingerprint = strm_runtime.prepare_strm_run_once({})
        with patch(
            "app.modules.scheduler.threading.Thread.start",
            side_effect=RuntimeError("synthetic thread failure"),
        ):
            result = strm_runtime.run_strm_once_confirmed({}, fingerprint)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "failed")
        self.assertIn("启动失败", result.error)
        self.assertFalse(self.scheduler._run_lock.locked())
        self.assertIsNone(self.scheduler._worker)
        self.assertFalse(self.scheduler._running)

    def test_actual_busy_admission_keeps_conflict_and_never_starts_a_worker(self):
        _preview, fingerprint = strm_runtime.prepare_strm_run_once({})
        self.scheduler._run_lock.acquire()
        try:
            result = strm_runtime.run_strm_once_confirmed({}, fingerprint)
        finally:
            self.scheduler._run_lock.release()
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "conflict")
        self.assertIn("已在运行", result.summary)
        self.assertIsNone(self.scheduler._worker)

    def test_scoped_success_and_stale_configuration_use_the_same_confirmed_path(self):
        sources = [{"id": "a", "name": "Alpha"}, {"id": "b", "name": "Beta"}]
        arguments = {"source_names": ["beta", "Alpha"]}
        with (
            patch(
                "app.modules.strm.configured_strm_source_plans",
                return_value=(sources, ""),
            ),
            patch.object(
                self.scheduler, "trigger", return_value={"ok": True}
            ) as trigger,
        ):
            preview, fingerprint = strm_runtime.prepare_strm_run_once(arguments)
            self.assertTrue(preview.ok)
            self.assertEqual(preview.data["source_count"], 2)
            result = strm_runtime.run_strm_once_confirmed(arguments, fingerprint)
            self.assertTrue(result.ok)
            self.assertEqual(
                result.effect_metadata["completion"],
                {
                    "kind": "strm_run",
                    "after_run_id": 0,
                    "trigger_type": "manual",
                    "operation": "run",
                },
            )
            trigger.assert_called_once_with("manual", selected_source_ids=["b", "a"])
            self.config.return_value = "changed"
            with self.assertRaises(AgentToolError) as error:
                strm_runtime.run_strm_once_confirmed(arguments, fingerprint)
            self.assertEqual(error.exception.code, "confirmation_stale")
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
                _preview, fingerprint = strm_runtime.prepare_strm_run_once({})
                result = strm_runtime.run_strm_once_confirmed({}, fingerprint)
                self.assertFalse(result.ok)
                self.assertEqual(result.status, "failed")
                self.assertNotIn("已在运行", result.summary)
                if reason.startswith("synthetic"):
                    self.assertNotIn(reason, result.error)
                else:
                    self.assertEqual(result.error, reason)
