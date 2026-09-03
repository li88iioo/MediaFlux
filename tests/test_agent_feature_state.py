"""Media Agent 非敏感功能开关确认动作测试。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config
from app.agent.errors import AgentToolError
from app.agent.feature_actions import (
    feature_state_arguments,
    prepare_feature_state_confirmation,
)
from app.agent.models import RiskLevel
from tests.agent_kernel_test_harness import (
    build_kernel_test_registry as build_tool_registry,
)


class FeatureStateUnitTests(unittest.TestCase):
    def test_arguments_are_strict_aliases_and_boolean_only(self):
        self.assertEqual(
            feature_state_arguments({"feature": "discovery", "enabled": False}),
            {"feature": "discovery", "enabled": False},
        )
        self.assertEqual(
            feature_state_arguments({"feature": "web_search", "enabled": True}),
            {"feature": "web_search", "enabled": True},
        )
        for arguments in (
            {},
            {"feature": "DISCOVERY_ENABLED", "enabled": True},
            {"feature": " Discovery ", "enabled": False},
            {"feature": " WEB_SEARCH ", "enabled": True},
            {"feature": "discovery", "enabled": 1},
            {"feature": "discovery", "enabled": "false"},
            {"feature": "discovery", "enabled": False, "key": "TMDB_API_KEY"},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(AgentToolError):
                feature_state_arguments(arguments)

    def test_registry_exposes_low_write_confirmation_gate(self):
        registry = build_tool_registry()
        capability = next(

                item
                for item in registry.capabilities()
                if item["name"] == "config.set_feature_state"

        )
        self.assertEqual(capability["risk"], RiskLevel.LOW_WRITE.value)
        self.assertTrue(capability["requires_confirmation"])
        with self.assertRaisesRegex(AgentToolError, "需要确认"):
            registry.execute(
                "config.set_feature_state", {"feature": "discovery", "enabled": False}
            )

    def test_preview_and_context_do_not_leak_file_contents(self):
        with tempfile.TemporaryDirectory() as root:
            env_file = Path(root) / "user.env"
            secret = "super-secret-token"
            config.write_env_file(
                env_file,
                {"DISCOVERY_ENABLED": "1", "TMDB_API_KEY": secret},
                replace=False,
            )
            with (
                patch.object(config, "ENV_FILE", env_file),
                patch.object(config, "_cache", None),
                patch.object(config, "_STARTUP_ENV_OVERRIDES", frozenset()),
                patch.dict(os.environ, {"DISCOVERY_ENABLED": ""}, clear=False),
                patch(
                    "app.agent.feature_actions.config.update_runtime_env_file"
                ) as writer,
            ):
                result, first = prepare_feature_state_confirmation(
                    {"feature": "discovery", "enabled": False}
                )
                first_preview = result
                second_preview, second = prepare_feature_state_confirmation(
                    {"feature": "discovery", "enabled": False}
                )
            self.assertTrue(result.ok)
            self.assertTrue(first_preview.ok)
            self.assertTrue(second_preview.ok)
            self.assertEqual(result.status, "confirmation_required")
            self.assertEqual(first, second)
            self.assertRegex(first, "^[0-9a-f]{64}$")
            self.assertNotIn(secret, str(result.to_dict()))
            self.assertNotIn(secret, first)
            self.assertNotIn("DISCOVERY_ENABLED", str(result.to_dict()))
            writer.assert_not_called()
