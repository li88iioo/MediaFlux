"""Media Agent 全库巡检策略受控配置测试。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from app import config
from app.agent.errors import AgentToolError
from app.agent.library_patrol_config_actions import (
    patrol_policy_arguments,
    prepare_patrol_policy_confirmation,
    summarize_patrol_policy,
)
from app.modules.agent_library_patrol_scheduler import AgentLibraryPatrolScheduler

_PATROL_KEYS = {
    "AGENT_LIBRARY_PATROL_ENABLED",
    "AGENT_LIBRARY_PATROL_NOTIFY_ENABLED",
    "AGENT_LIBRARY_PATROL_INTERVAL_HOURS",
    "AGENT_LIBRARY_PATROL_MAX_SERIES",
}


class PatrolPolicyUnitTests(unittest.TestCase):
    def test_arguments_are_partial_bounded_and_strict(self):
        self.assertEqual(
            patrol_policy_arguments(
                {
                    "enabled": True,
                    "notify_enabled": False,
                    "interval_hours": 168,
                    "max_series": 100,
                }
            ),
            {
                "enabled": True,
                "notify_enabled": False,
                "interval_hours": 168,
                "max_series": 100,
            },
        )
        for arguments in (
            {},
            {"enabled": 1},
            {"notify_enabled": "true"},
            {"interval_hours": True},
            {"interval_hours": 0},
            {"interval_hours": 169},
            {"max_series": 0},
            {"max_series": 101},
            {"key": "AGENT_LIBRARY_PATROL_ENABLED"},
            {"token": "secret"},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(AgentToolError):
                patrol_policy_arguments(arguments)

    def test_summary_preview_and_context_do_not_leak_other_config(self):
        with tempfile.TemporaryDirectory() as root:
            env_file = Path(root) / "user.env"
            secret = "patrol-secret-must-not-leak"
            config.write_env_file(
                env_file,
                {
                    "AGENT_LIBRARY_PATROL_ENABLED": "0",
                    "AGENT_LIBRARY_PATROL_NOTIFY_ENABLED": "1",
                    "AGENT_LIBRARY_PATROL_INTERVAL_HOURS": "12",
                    "AGENT_LIBRARY_PATROL_MAX_SERIES": "40",
                    "TMDB_API_KEY": secret,
                },
                replace=False,
            )
            with (
                patch.object(config, "ENV_FILE", env_file),
                patch.object(config, "_cache", None),
                patch.object(config, "_STARTUP_ENV_OVERRIDES", frozenset()),
            ):
                summary = summarize_patrol_policy({})
                preview, context = prepare_patrol_policy_confirmation(
                    {"notify_enabled": False}
                )
        rendered = repr((summary.to_dict(), preview.to_dict(), context))
        self.assertNotIn(secret, rendered)
        self.assertNotIn("TMDB_API_KEY", rendered)
        self.assertNotIn(str(env_file), rendered)
        self.assertEqual(len(context), 64)
        self.assertIn("丢弃", " ".join(preview.suggestions))

    def test_notification_backlog_side_effect_is_disclosed_while_already_disabled(self):
        with tempfile.TemporaryDirectory() as root:
            env_file = Path(root) / "user.env"
            config.write_env_file(
                env_file,
                {
                    "AGENT_LIBRARY_PATROL_ENABLED": "0",
                    "AGENT_LIBRARY_PATROL_NOTIFY_ENABLED": "0",
                    "AGENT_LIBRARY_PATROL_INTERVAL_HOURS": "24",
                    "AGENT_LIBRARY_PATROL_MAX_SERIES": "50",
                },
                replace=False,
            )
            with (
                patch.object(config, "ENV_FILE", env_file),
                patch.object(config, "_cache", None),
                patch.object(config, "_STARTUP_ENV_OVERRIDES", frozenset()),
            ):
                preview, context = prepare_patrol_policy_confirmation({"enabled": True})
        self.assertTrue(preview.ok)
        self.assertEqual(len(context), 64)
        self.assertIn("积压", " ".join(preview.suggestions))
        self.assertIn("无法恢复", " ".join(preview.suggestions))

    def test_scheduler_can_reload_without_immediate_patrol(self):
        scheduler = AgentLibraryPatrolScheduler(
            clock=lambda: datetime(2026, 8, 4, 12, 0, 0)
        )
        with (
            patch.object(scheduler, "_enabled", return_value=True),
            patch.object(scheduler, "_interval_seconds", return_value=6 * 60 * 60),
            patch.object(scheduler, "_notifications_enabled", return_value=False),
            patch(
                "app.modules.agent_library_patrol_scheduler.db.reschedule_agent_library_patrol"
            ) as reschedule,
            patch(
                "app.modules.agent_library_patrol_scheduler.db.discard_agent_library_patrol_notifications"
            ) as discard_notifications,
        ):
            scheduler.reload(immediate=False)
        reschedule.assert_called_once_with(next_run_at="2026-08-04 18:00:00")
        discard_notifications.assert_called_once_with()
        self.assertTrue(scheduler._wake_event.is_set())
