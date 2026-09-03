"""Media Agent 非敏感白名单策略的只读与确认写入测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config
from app.agent.errors import AgentToolError
from app.agent.safe_policy_actions import (
    prepare_safe_policy_confirmation,
    safe_policy_arguments,
    safe_policy_summary_arguments,
    summarize_safe_policies,
)

_POLICY_KEYS = {
    "TMDB_MATCH_MODE",
    "LOGIN_WALLPAPER_MODE",
    "TAVILY_SEARCH_DEPTH",
    "TAVILY_MAX_RESULTS",
    "TAVILY_CACHE_TTL_SECONDS",
    "TAVILY_DAILY_CREDIT_LIMIT",
    "TAVILY_TIMEOUT_SECONDS",
    "DISCOVERY_CACHE_TTL_SECONDS",
    "DISCOVERY_STALE_TTL_SECONDS",
    "DOUBAN_CACHE_TTL_SECONDS",
    "INDEXER_BTBTLA_MIN_INTERVAL_SECONDS",
    "TMDB_API_KEY",
}


class SafePolicyUnitTests(unittest.TestCase):
    def test_arguments_are_strict_and_bounded(self):
        self.assertEqual(safe_policy_summary_arguments({}), {})
        self.assertEqual(
            safe_policy_arguments({"policy": "tmdb_match_mode", "value": " STRICT "}),
            {"policy": "tmdb_match_mode", "value": "strict"},
        )
        self.assertEqual(
            safe_policy_arguments({"policy": "web_search_max_results", "value": 10}),
            {"policy": "web_search_max_results", "value": 10},
        )
        self.assertEqual(
            safe_policy_arguments(
                {"policy": "web_search_daily_credit_limit", "value": 100000}
            ),
            {"policy": "web_search_daily_credit_limit", "value": 100000},
        )
        invalid = (
            {"extra": True},
            {},
            {"policy": "unknown", "value": "strict"},
            {"policy": "tmdb_match_mode", "value": "fast"},
            {"policy": "web_search_max_results", "value": True},
            {"policy": "web_search_max_results", "value": 0},
            {"policy": "web_search_timeout_seconds", "value": 31},
            {"policy": "web_search_cache_ttl_seconds", "value": 29},
            {"policy": "web_search_daily_credit_limit", "value": 100001},
            {"policy": "discovery_cache_ttl_seconds", "value": 59},
            {"policy": "discovery_stale_ttl_seconds", "value": 2592001},
            {"policy": "douban_cache_ttl_seconds", "value": 299},
            {"policy": "indexer_btbtla_min_interval_seconds", "value": -1},
            {"policy": "tmdb_match_mode", "value": "strict", "key": "TMDB_API_KEY"},
        )
        for arguments in invalid:
            with self.subTest(arguments=arguments), self.assertRaises(AgentToolError):
                if arguments == {"extra": True}:
                    safe_policy_summary_arguments(arguments)
                else:
                    safe_policy_arguments(arguments)

    def test_summary_and_preview_never_expose_unrelated_config(self):
        with tempfile.TemporaryDirectory() as root:
            env_file = Path(root) / "user.env"
            secret = "safe-policy-secret-must-not-leak"
            config.write_env_file(
                env_file,
                {
                    "TMDB_MATCH_MODE": "strict",
                    "LOGIN_WALLPAPER_MODE": "default",
                    "TAVILY_SEARCH_DEPTH": "basic",
                    "TAVILY_MAX_RESULTS": "5",
                    "TAVILY_CACHE_TTL_SECONDS": "900",
                    "TAVILY_DAILY_CREDIT_LIMIT": "100",
                    "TAVILY_TIMEOUT_SECONDS": "10",
                    "DISCOVERY_CACHE_TTL_SECONDS": "21600",
                    "DISCOVERY_STALE_TTL_SECONDS": "604800",
                    "DOUBAN_CACHE_TTL_SECONDS": "21600",
                    "INDEXER_BTBTLA_MIN_INTERVAL_SECONDS": "5",
                    "TMDB_API_KEY": secret,
                },
                replace=False,
            )
            with (
                patch.object(config, "ENV_FILE", env_file),
                patch.object(config, "_cache", None),
                patch.object(config, "_STARTUP_ENV_OVERRIDES", frozenset()),
            ):
                summary = summarize_safe_policies({})
                preview, context = prepare_safe_policy_confirmation(
                    {"policy": "web_search_timeout_seconds", "value": 15}
                )
        rendered = repr((summary.to_dict(), preview.to_dict(), context))
        self.assertTrue(summary.ok)
        self.assertEqual(summary.data["policy_count"], 11)
        self.assertTrue(preview.ok)
        self.assertRegex(context, "^[0-9a-f]{64}$")
        self.assertNotIn(secret, rendered)
        self.assertNotIn("TMDB_API_KEY", rendered)
        self.assertNotIn(str(env_file), rendered)

    def test_discovery_cache_ttls_keep_safe_ordering(self):
        with tempfile.TemporaryDirectory() as root:
            env_file = Path(root) / "user.env"
            config.write_env_file(
                env_file,
                {
                    "DISCOVERY_CACHE_TTL_SECONDS": "21600",
                    "DISCOVERY_STALE_TTL_SECONDS": "604800",
                },
                replace=False,
            )
            with (
                patch.object(config, "ENV_FILE", env_file),
                patch.object(config, "_cache", None),
                patch.object(config, "_STARTUP_ENV_OVERRIDES", frozenset()),
            ):
                too_long, _ = prepare_safe_policy_confirmation(
                    {"policy": "discovery_cache_ttl_seconds", "value": 604801}
                )
                too_short, _ = prepare_safe_policy_confirmation(
                    {"policy": "discovery_stale_ttl_seconds", "value": 21599}
                )
                valid, _ = prepare_safe_policy_confirmation(
                    {"policy": "discovery_stale_ttl_seconds", "value": 86400}
                )
        self.assertFalse(too_long.ok)
        self.assertEqual(too_long.status, "precondition_failed")
        self.assertFalse(too_short.ok)
        self.assertEqual(too_short.status, "precondition_failed")
        self.assertTrue(valid.ok)
