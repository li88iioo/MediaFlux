"""Media Agent 非敏感白名单策略的只读与确认写入测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from app import config
from app.modules.policy_config import normalize_policy, policies, prepare_policy

_POLICY_KEYS = {
    "TMDB_MATCH_MODE",
    "LOGIN_WALLPAPER_MODE",
    "TAVILY_SEARCH_DEPTH",
    "TAVILY_MAX_RESULTS",
    "TAVILY_CACHE_TTL_SECONDS",
    "TAVILY_DAILY_CREDIT_LIMIT",
    "TAVILY_TIMEOUT_SECONDS",
    "WEB_READ_TIMEOUT_SECONDS",
    "WEB_READ_CACHE_TTL_SECONDS",
    "DISCOVERY_CACHE_TTL_SECONDS",
    "DISCOVERY_STALE_TTL_SECONDS",
    "DOUBAN_CACHE_TTL_SECONDS",
    "INDEXER_BTBTLA_MIN_INTERVAL_SECONDS",
    "TMDB_API_KEY",
}


class SafePolicyUnitTests(unittest.TestCase):
    def test_arguments_are_strict_and_bounded(self):
        with self.assertRaises(TypeError):
            extra: dict[str, Any] = {"extra": True}
            policies(**extra)
        self.assertEqual(
            normalize_policy(policy="tmdb_match_mode", value=" STRICT "),
            {"policy": "tmdb_match_mode", "value": "strict"},
        )
        self.assertEqual(
            normalize_policy(policy="web_search_max_results", value=10),
            {"policy": "web_search_max_results", "value": 10},
        )
        self.assertEqual(
            normalize_policy(policy="web_search_daily_credit_limit", value=100000
            ),
            {"policy": "web_search_daily_credit_limit", "value": 100000},
        )
        invalid: tuple[dict[str, Any], ...] = (
            {"extra": True},
            {},
            {"policy": "unknown", "value": "strict"},
            {"policy": "tmdb_match_mode", "value": "fast"},
            {"policy": "web_search_max_results", "value": True},
            {"policy": "web_search_max_results", "value": 0},
            {"policy": "web_search_timeout_seconds", "value": 31},
            {"policy": "web_read_timeout_seconds", "value": 1},
            {"policy": "web_read_cache_ttl_seconds", "value": 86401},
            {"policy": "web_search_cache_ttl_seconds", "value": 29},
            {"policy": "web_search_daily_credit_limit", "value": 100001},
            {"policy": "discovery_cache_ttl_seconds", "value": 59},
            {"policy": "discovery_stale_ttl_seconds", "value": 2592001},
            {"policy": "douban_cache_ttl_seconds", "value": 299},
            {"policy": "indexer_btbtla_min_interval_seconds", "value": -1},
            {"policy": "tmdb_match_mode", "value": "strict", "key": "TMDB_API_KEY"},
        )
        for arguments in invalid:
            with self.subTest(arguments=arguments), self.assertRaises((ValueError, TypeError)):
                if arguments == {"extra": True}:
                    policies(**arguments)
                else:
                    normalize_policy(**arguments)

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
                    "DISCOVERY_STALE_TTL_SECONDS": "604700",
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
                summary = policies()
                prepared = prepare_policy(policy="web_search_timeout_seconds", value=15
                )
        preview, context = prepared["preview"], prepared["fingerprint"]
        rendered = repr((summary, preview, context))
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["data"]["policy_count"], 13)
        self.assertTrue(preview["ok"])
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
                    "DISCOVERY_STALE_TTL_SECONDS": "604700",
                },
                replace=False,
            )
            with (
                patch.object(config, "ENV_FILE", env_file),
                patch.object(config, "_cache", None),
                patch.object(config, "_STARTUP_ENV_OVERRIDES", frozenset()),
            ):
                too_long = prepare_policy(policy="discovery_cache_ttl_seconds", value=604800
                )
                too_short = prepare_policy(policy="discovery_stale_ttl_seconds", value=21599
                )
                valid = prepare_policy(policy="discovery_stale_ttl_seconds", value=86400
                )
        self.assertFalse(too_long["preview"]["ok"])
        self.assertEqual(too_long["preview"]["status"], "precondition_failed")
        self.assertFalse(too_short["preview"]["ok"])
        self.assertEqual(too_short["preview"]["status"], "precondition_failed")
        self.assertTrue(valid["preview"]["ok"])
