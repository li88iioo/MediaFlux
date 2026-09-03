"""Media Agent 功能状态摘要的投影、路由、安全与 API 契约。"""

from __future__ import annotations

import json
from unittest.mock import patch

from app.agent.errors import AgentToolError
from app.agent.feature_actions import (
    feature_summary_arguments,
    summarize_feature_states,
)
from tests.support import IsolatedDatabaseTestCase


class FeatureSummaryUnitTests(IsolatedDatabaseTestCase):
    def test_arguments_are_strictly_empty(self):
        self.assertEqual(feature_summary_arguments({}), {})
        for arguments in (None, {"feature": "discovery"}, {"debug": True}):
            with self.subTest(arguments=arguments), self.assertRaises(AgentToolError):
                feature_summary_arguments(arguments)

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
            patch("app.agent.feature_actions.config.get_bool", side_effect=get_bool),
            patch("app.agent.feature_actions.config.get", side_effect=get_value),
            patch(
                "app.agent.feature_actions.config.has_external_override",
                side_effect=lambda key: key == "DISCOVERY_DOUBAN_ENABLED",
            ),
        ):
            result = summarize_feature_states({})
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "attention")
        self.assertEqual(result.data["feature_count"], 10)
        self.assertEqual(result.data["enabled_count"], 6)
        self.assertEqual(result.data["available_count"], 3)
        self.assertEqual(result.data["disabled_count"], 4)
        self.assertEqual(result.data["attention_count"], 3)
        by_feature = {item["feature"]: item for item in result.data["features"]}
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
        serialized = json.dumps(result.to_dict(), ensure_ascii=False)
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
                "app.agent.feature_actions.config.get_bool",
                side_effect=lambda key, default=False: enabled.get(key, default),
            ),
            patch("app.agent.feature_actions.config.get", return_value=""),
            patch(
                "app.agent.feature_actions.config.has_external_override",
                return_value=False,
            ),
        ):
            result = summarize_feature_states({})
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.data["available_count"], 7)
        with (
            patch("app.agent.feature_actions.config.get_bool", return_value=False),
            patch("app.agent.feature_actions.config.get", return_value=""),
            patch(
                "app.agent.feature_actions.config.has_external_override",
                return_value=False,
            ),
        ):
            disabled = summarize_feature_states({})
        self.assertEqual(disabled.status, "disabled")
        self.assertEqual(disabled.data["available_count"], 0)
        self.assertEqual(disabled.data["disabled_count"], 10)
