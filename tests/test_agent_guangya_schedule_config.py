"""Media Agent 光鸭连接与定时整理策略受控配置测试。"""

from __future__ import annotations
from datetime import datetime

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app import config, sdk
from app.modules.schedule_policy import normalize_changes, prepare_policy_change, summarize_policy
from app.modules.organize_scheduler import OrganizeScheduler

_GUANGYA_SCHEDULE_KEYS = {
    "GY_ORGANIZE_SCHEDULE_ENABLED",
    "GY_ORGANIZE_SCHEDULE_CRON",
    "GY_ORGANIZE_NOTIFY_ENABLED",
}


class GuangyaSchedulePolicyUnitTests(unittest.TestCase):
    def test_arguments_are_partial_bounded_and_strict(self):
        self.assertEqual(
            normalize_changes("organize",
                {"enabled": True, "cron": "  30   2 * * * ", "notify_enabled": False}
            ),
            {"enabled": True, "cron": "30 2 * * *", "notify_enabled": False},
        )
        for arguments in (
            {},
            {"enabled": 1},
            {"notify_enabled": "true"},
            {"cron": 4},
            {"cron": ""},
            {"cron": "0 4 * *"},
            {"cron": "0 4 * * * *"},
            {"key": "GY_ORGANIZE_SCHEDULE_ENABLED"},
            {"token": "secret"},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                normalize_changes("organize", arguments)

    def test_connection_status_preserves_unknown_and_never_leaks_credentials(self):
        secret = "guangya-secret-must-not-leak"
        client = Mock()
        client.logged_in = True
        client.probe_connection.return_value = True
        client.token = secret
        client.phone = "13800000000"
        with patch(
            "app.modules.guangya_account.GuangYaClient",
            return_value=client,
        ):
            result = sdk.cloud.connection()
        rendered = repr(result)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "ready")
        client.probe_connection.assert_called_once_with()
        client.validate.assert_not_called()
        self.assertNotIn(secret, rendered)
        self.assertNotIn("13800000000", rendered)
        self.assertNotIn("token", rendered.casefold())

        client.probe_connection.reset_mock()
        client.probe_connection.return_value = False
        with patch(
            "app.modules.guangya_account.GuangYaClient",
            return_value=client,
        ):
            unavailable = sdk.cloud.connection()
        self.assertFalse(unavailable["ok"])
        self.assertEqual(unavailable["status"], "attention")

        client.probe_connection.side_effect = RuntimeError(f"private {secret}")
        client.close.reset_mock()
        with patch("app.modules.guangya_account.GuangYaClient", return_value=client):
            interrupted = sdk.cloud.connection()
        self.assertEqual(interrupted["status"], "unavailable")
        self.assertTrue(interrupted["configured"])
        self.assertIsNone(interrupted["reachable"])
        client.close.assert_called_once_with()

        with patch(
            "app.modules.guangya_account.GuangYaClient",
            side_effect=RuntimeError(f"private {secret}"),
        ):
            failed = sdk.cloud.connection()
        failed_rendered = repr(failed)
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["status"], "unavailable")
        self.assertIsNone(failed["configured"])
        self.assertIsNone(failed["reachable"])
        self.assertNotIn(secret, failed_rendered)
        self.assertNotIn("RuntimeError", failed_rendered)

    def test_summary_preview_context_do_not_leak_other_config(self):
        with tempfile.TemporaryDirectory() as root:
            env_file = Path(root) / "user.env"
            secret = "guangya-secret-must-not-leak"
            config.write_env_file(
                env_file,
                {
                    "GY_ORGANIZE_SCHEDULE_ENABLED": "0",
                    "GY_ORGANIZE_SCHEDULE_CRON": "0 4 * * *",
                    "GY_ORGANIZE_NOTIFY_ENABLED": "1",
                    "GY_TOKEN": secret,
                    "GY_ORGANIZE_SOURCE_DIRS": '[{"id":"private"}]',
                },
                replace=False,
            )
            scheduler = Mock()
            scheduler.status.return_value = {
                "cron_valid": True,
                "config_error": "",
                "next_run": "",
            }
            with (
                patch.object(config, "ENV_FILE", env_file),
                patch.object(config, "_cache", None),
                patch.object(config, "_STARTUP_ENV_OVERRIDES", frozenset()),
                patch(
                    "app.modules.schedule_policy.get_organize_scheduler",
                    return_value=scheduler,
                ),
            ):
                summary = summarize_policy("organize")
                preview, context = (
                    prepare_policy_change("organize",
                        {"enabled": True}
                    )
                )
        rendered = repr((summary, preview, context))
        self.assertNotIn(secret, rendered)
        self.assertNotIn("GY_TOKEN", rendered)
        self.assertNotIn("private", rendered)
        self.assertNotIn(str(env_file), rendered)
        self.assertEqual(len(context), 64)
        self.assertIn("不会立即", " ".join(preview["suggestions"]))

    def test_scheduler_reload_only_invalidates_schedule(self):
        manager = Mock()
        scheduler = OrganizeScheduler(manager=manager)
        scheduler._next_run = datetime(2030, 1, 1)
        scheduler._loaded_cron = "0 4 * * *"
        scheduler.reload()
        self.assertIsNone(scheduler._next_run)
        self.assertEqual(scheduler._loaded_cron, "")
        self.assertTrue(scheduler._wake_event.is_set())
        manager.start.assert_not_called()
