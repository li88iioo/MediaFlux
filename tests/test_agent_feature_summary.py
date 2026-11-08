"""Media Agent 功能状态摘要的投影、路由、安全与 API 契约。"""

from __future__ import annotations

import json
from unittest.mock import patch

from app import sdk
from app.modules.feature_config import features
from tests.support import initialized_web_runtime
import unittest


class FeatureSummaryUnitTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(initialized_web_runtime())

    def test_arguments_are_strictly_empty(self):
        self.assertEqual(sdk.prepare_sdk_method("settings.features", {}, owner="feature-read"), {})
        for arguments in ({"feature": "discovery"}, {"debug": True}):
            with self.subTest(arguments=arguments), self.assertRaises(TypeError):
                sdk.prepare_sdk_method("settings.features", arguments, owner="feature-read")

    def test_summary_projects_dependency_state_without_values(self):
        values = {
            "DISCOVERY_ENABLED": False,
            "DISCOVERY_DOUBAN_ENABLED": True,
            "DISCOVERY_RESOURCE_RESULTS_ENABLED": True,
            "INDEXER_SEARCH_ENABLED": False,
            "WEB_SEARCH_ENABLED": True,
        }
        secret = "FEATURE_SUMMARY_SECRET"

        def get_bool(key: str, default: bool = False) -> bool:
            return values.get(key, default)

        def get_value(key: str, default: str = "") -> str:
            if key == "INDEXER_ENABLED_SITES":
                return f"unknown,{secret}"
            return default

        with (
            patch("app.modules.feature_config.config.get_bool", side_effect=get_bool),
            patch("app.modules.feature_config.config.get", side_effect=get_value),
            patch(
                "app.modules.feature_config.config.has_external_override",
                side_effect=lambda key: key == "DISCOVERY_DOUBAN_ENABLED",
            ),
        ):
            result = features()
        self.assertEqual({row["feature"] for row in result["features"]}, {"discovery", "douban", "resource_results", "indexer_search", "web_search", "web_read", "offline_magnet", "offline_ed2k", "offline_http", "strm_metadata", "download_verification_notify"})
        self.assertEqual(result["status"], "attention")
        self.assertEqual(result["feature_count"], 11)
        self.assertEqual(result["enabled_count"], 7)
        self.assertEqual(result["available_count"], 4)
        self.assertEqual(result["disabled_count"], 4)
        self.assertEqual(result["attention_count"], 3)
        by_feature = {item["feature"]: item for item in result["features"]}
        self.assertEqual(by_feature["discovery"]["availability"], "disabled")
        self.assertEqual(by_feature["discovery"]["reason_codes"], ["feature_disabled"])
        self.assertEqual(by_feature["douban"]["availability"], "blocked")
        self.assertEqual(by_feature["douban"]["reason_codes"], ["parent_disabled"])
        self.assertTrue(by_feature["douban"]["managed_by_environment"])
        self.assertEqual(
            by_feature["resource_results"]["reason_codes"],
            ["parent_disabled", "search_disabled", "no_enabled_sites"],
        )
        self.assertEqual(by_feature["indexer_search"]["availability"], "disabled")
        self.assertEqual(by_feature["web_search"]["availability"], "blocked")
        self.assertEqual(
            by_feature["web_search"]["reason_codes"], ["provider_not_configured"]
        )
        serialized = json.dumps(result, ensure_ascii=False)
        for forbidden in (
            secret,
            "DISCOVERY_ENABLED",
            "INDEXER_ENABLED_SITES",
            "unknown",
            "TMDB_API_KEY",
            "http://",
            "/srv/",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_sukebei_opt_in_counts_as_an_effective_site_and_all_disabled_is_explicit(
        self,
    ):
        enabled = {
            "DISCOVERY_ENABLED": True,
            "DISCOVERY_DOUBAN_ENABLED": True,
            "DISCOVERY_RESOURCE_RESULTS_ENABLED": True,
            "INDEXER_SEARCH_ENABLED": True,
            "INDEXER_SUKEBEI_ENABLED": True,
        }
        with (
            patch(
                "app.modules.feature_config.config.get_bool",
                side_effect=lambda key, default=False: enabled.get(key, default),
            ),
            patch("app.modules.feature_config.config.get", return_value=""),
            patch(
                "app.modules.feature_config.config.has_external_override",
                return_value=False,
            ),
        ):
            result = features()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["available_count"], 8)
        with (
            patch("app.modules.feature_config.config.get_bool", return_value=False),
            patch("app.modules.feature_config.config.get", return_value=""),
            patch(
                "app.modules.feature_config.config.has_external_override",
                return_value=False,
            ),
        ):
            disabled = features()
        self.assertEqual(disabled["status"], "disabled")
        self.assertEqual(disabled["available_count"], 0)
        self.assertEqual(disabled["disabled_count"], 11)
