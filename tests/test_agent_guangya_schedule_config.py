"""Media Agent 光鸭连接与定时整理策略受控配置测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app import config
from app.agent.errors import AgentToolError
from app.agent.guangya_schedule_config_actions import (
    get_guangya_connection_status,
    guangya_organize_schedule_policy_arguments,
    prepare_guangya_organize_schedule_policy_confirmation,
    summarize_guangya_organize_schedule_policy,
)
from app.modules.organize_scheduler import OrganizeScheduler

_GUANGYA_SCHEDULE_KEYS = {
    "GY_ORGANIZE_SCHEDULE_ENABLED",
    "GY_ORGANIZE_SCHEDULE_CRON",
    "GY_ORGANIZE_NOTIFY_ENABLED",
}


class GuangyaSchedulePolicyUnitTests(unittest.TestCase):
    def test_arguments_are_partial_bounded_and_strict(self):
        self.assertEqual(
            guangya_organize_schedule_policy_arguments(
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
            with self.subTest(arguments=arguments), self.assertRaises(AgentToolError):
                guangya_organize_schedule_policy_arguments(arguments)

    def test_connection_status_is_bounded_and_never_leaks_credentials(self):
        secret = "guangya-secret-must-not-leak"
        client = Mock()
        client.logged_in = True
        client.validate.return_value = True
        client.token = secret
        client.phone = "13800000000"
        with patch(
            "app.agent.guangya_schedule_config_actions.GuangYaClient",
            return_value=client,
        ):
            result = get_guangya_connection_status({})
        rendered = repr(result.to_dict())
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "ready")
        self.assertNotIn(secret, rendered)
        self.assertNotIn("13800000000", rendered)
        self.assertNotIn("token", rendered.casefold())
        with patch(
            "app.agent.guangya_schedule_config_actions.GuangYaClient",
            side_effect=RuntimeError(f"private {secret}"),
        ):
            failed = get_guangya_connection_status({})
        failed_rendered = repr(failed.to_dict())
        self.assertFalse(failed.ok)
        self.assertEqual(failed.status, "unavailable")
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
                    "app.agent.guangya_schedule_config_actions.get_organize_scheduler",
                    return_value=scheduler,
                ),
            ):
                summary = summarize_guangya_organize_schedule_policy({})
                preview, context = (
                    prepare_guangya_organize_schedule_policy_confirmation(
                        {"enabled": True}
                    )
                )
        rendered = repr((summary.to_dict(), preview.to_dict(), context))
        self.assertNotIn(secret, rendered)
        self.assertNotIn("GY_TOKEN", rendered)
        self.assertNotIn("private", rendered)
        self.assertNotIn(str(env_file), rendered)
        self.assertEqual(len(context), 64)
        self.assertIn("不会立即", " ".join(preview.suggestions))

    def test_scheduler_reload_only_invalidates_schedule(self):
        manager = Mock()
        scheduler = OrganizeScheduler(manager=manager)
        scheduler._next_run = "future"
        scheduler._loaded_cron = "0 4 * * *"
        scheduler.reload()
        self.assertIsNone(scheduler._next_run)
        self.assertEqual(scheduler._loaded_cron, "")
        self.assertTrue(scheduler._wake_event.is_set())
        manager.start.assert_not_called()
