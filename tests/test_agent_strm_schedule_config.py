"""Media Agent STRM 定时同步策略受控配置测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config
from app.agent import strm_schedule_config_actions
from app.agent.errors import AgentToolError
from app.agent.strm_schedule_config_actions import (
    prepare_strm_schedule_policy_confirmation,
    strm_schedule_policy_arguments,
    summarize_strm_schedule_policy,
)
from app.modules.scheduler import STRMScheduler
from tests.agent_kernel_test_harness import get_kernel_test_service as get_agent_service

_STRM_KEYS = {"STRM_SCHEDULE_ENABLED", "STRM_SCHEDULE_CRON", "STRM_NOTIFY_ENABLED"}


class StrmSchedulePolicyUnitTests(unittest.TestCase):
    def test_arguments_are_partial_bounded_and_strict(self):
        self.assertEqual(
            strm_schedule_policy_arguments(
                {"enabled": True, "cron": "  0   4 * * * ", "notify_enabled": False}
            ),
            {"enabled": True, "cron": "0 4 * * *", "notify_enabled": False},
        )
        for arguments in (
            {},
            {"enabled": 1},
            {"notify_enabled": "true"},
            {"cron": 4},
            {"cron": ""},
            {"cron": "0 4 * *"},
            {"cron": "0 4 * * * *"},
            {"key": "STRM_SCHEDULE_ENABLED"},
            {"token": "secret"},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(AgentToolError):
                strm_schedule_policy_arguments(arguments)

    def test_summary_preview_context_do_not_leak_other_config(self):
        with tempfile.TemporaryDirectory() as root:
            env_file = Path(root) / "user.env"
            secret = "strm-secret-must-not-leak"
            config.write_env_file(
                env_file,
                {
                    "STRM_SCHEDULE_ENABLED": "0",
                    "STRM_SCHEDULE_CRON": "0 4 * * *",
                    "STRM_NOTIFY_ENABLED": "1",
                    "GY_TOKEN": secret,
                },
                replace=False,
            )
            with (
                patch.object(config, "ENV_FILE", env_file),
                patch.object(config, "_cache", None),
                patch.object(config, "_STARTUP_ENV_OVERRIDES", frozenset()),
            ):
                summary = summarize_strm_schedule_policy({})
                preview, context = prepare_strm_schedule_policy_confirmation(
                    {"enabled": True}
                )
        rendered = repr((summary.to_dict(), preview.to_dict(), context))
        self.assertNotIn(secret, rendered)
        self.assertNotIn("GY_TOKEN", rendered)
        self.assertNotIn(str(env_file), rendered)
        self.assertEqual(len(context), 64)
        self.assertIn("不会立即", " ".join(preview.suggestions))

    def test_confirmation_preparer_uses_one_snapshot(self):
        with tempfile.TemporaryDirectory() as root:
            env_file = Path(root) / "user.env"
            config.write_env_file(
                env_file,
                {
                    "STRM_SCHEDULE_ENABLED": "0",
                    "STRM_SCHEDULE_CRON": "0 4 * * *",
                    "STRM_NOTIFY_ENABLED": "1",
                },
                replace=False,
            )
            with (
                patch.object(config, "ENV_FILE", env_file),
                patch.object(config, "_cache", None),
                patch.object(config, "_STARTUP_ENV_OVERRIDES", frozenset()),
                patch.object(
                    strm_schedule_config_actions,
                    "_capture",
                    wraps=strm_schedule_config_actions._capture,
                ) as capture,
            ):
                prepared = get_agent_service().prepare(
                    "strm.set_schedule_policy",
                    {"enabled": True},
                    owner="owner",
                )
        self.assertEqual(prepared["tool_call"]["name"], "strm.set_schedule_policy")
        self.assertEqual(prepared["tool_call"]["argument_keys"], ["enabled"])
        self.assertTrue(prepared["result"]["ok"])
        self.assertEqual(capture.call_count, 1)

    def test_scheduler_reload_only_invalidates_schedule(self):
        scheduler = STRMScheduler()
        scheduler._next_run = "future"
        scheduler._loaded_cron = "0 4 * * *"
        with patch.object(scheduler, "trigger") as trigger:
            scheduler.reload()
        self.assertIsNone(scheduler._next_run)
        self.assertEqual(scheduler._loaded_cron, "")
        self.assertTrue(scheduler._wake_event.is_set())
        trigger.assert_not_called()
