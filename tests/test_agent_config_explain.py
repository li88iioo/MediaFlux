"""Media Agent 配置组件说明的安全投影、路由与 API 契约。"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from app.agent.config_explain_actions import (
    config_component_arguments,
    explain_config_component,
)
from app.agent.errors import AgentToolError
from app.agent.models import ToolResult


def _config_get(values: dict[str, object]):
    return lambda key, default="": values.get(key, default)


def _explain_with_values(component: str, values: dict[str, object]) -> ToolResult:
    with (
        patch(
            "app.agent.config_explain_actions.config.all_items",
            return_value=dict(values),
        ),
        patch(
            "app.agent.config_explain_actions.config.get",
            side_effect=_config_get(values),
        ),
        patch(
            "app.agent.config_explain_actions.config.has_external_override",
            return_value=False,
        ),
    ):
        return explain_config_component({"component": component})


class ConfigComponentExplainUnitTests(unittest.TestCase):
    def test_arguments_are_strict_lowercase_whitelist(self):
        self.assertEqual(
            config_component_arguments({"component": " jellyfin "}),
            {"component": "jellyfin"},
        )
        invalid = (
            None,
            {},
            {"component": ""},
            {"component": 1},
            {"component": "JELLYFIN"},
            {"component": "plex"},
            {"component": "jellyfin", "url": "http://attacker.invalid"},
            {"component": "jellyfin", "value": "secret"},
        )
        for arguments in invalid:
            with self.subTest(arguments=arguments), self.assertRaises(AgentToolError):
                config_component_arguments(arguments)

    def test_incomplete_component_returns_labels_without_configuration_values(self):
        values = {
            "QB_URL": "http://private-qb.example:8080",
            "QB_USERNAME": "",
            "QB_PASSWORD": "TOP_SECRET_PASSWORD",
            "QB_API_KEY": "",
            "UNRELATED_PATH": "/srv/private/media",
        }
        result = _explain_with_values("qbittorrent", values)
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "incomplete")
        self.assertEqual(result.summary, "qBittorrent：配置不完整")
        self.assertEqual(
            result.data["required_field_labels"], ["服务地址", "API Key 或用户名/密码"]
        )
        self.assertEqual(result.data["missing_field_labels"], ["API Key 或用户名/密码"])
        self.assertIn("提交资源到 qBittorrent", result.data["blocked_capabilities"])
        serialized = json.dumps(result.to_dict(), ensure_ascii=False)
        for forbidden in (
            "QB_URL",
            "QB_PASSWORD",
            "TOP_SECRET_PASSWORD",
            "private-qb.example",
            "/srv/private/media",
            "UNRELATED_PATH",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_disabled_switch_wins_over_retained_credentials(self):
        result = _explain_with_values(
            "jellyfin",
            {
                "JELLYFIN_ENABLED": "0",
                "JELLYFIN_URL": "http://jellyfin.internal:8096",
                "JELLYFIN_API_KEY": "retained-secret",
            },
        )
        self.assertEqual(result.status, "disabled")
        self.assertEqual(result.summary, "Jellyfin：已关闭")
        self.assertFalse(result.data["enabled"])
        self.assertEqual(result.data["missing_field_labels"], [])
        self.assertTrue(result.data["blocked_capabilities"])
        self.assertNotIn(
            "retained-secret", json.dumps(result.to_dict(), ensure_ascii=False)
        )

    def test_base_component_reports_environment_management_without_keys(self):
        values = {
            "JELLYFIN_ENABLED": "1",
            "JELLYFIN_URL": "http://jellyfin.internal:8096",
            "JELLYFIN_API_KEY": "",
        }
        with (
            patch(
                "app.agent.config_explain_actions.config.all_items",
                return_value=dict(values),
            ),
            patch(
                "app.agent.config_explain_actions.config.get",
                side_effect=_config_get(values),
            ),
            patch(
                "app.agent.config_explain_actions.config.has_external_override",
                side_effect=lambda key: key == "JELLYFIN_API_KEY",
            ),
        ):
            result = explain_config_component({"component": "jellyfin"})
        self.assertEqual(result.status, "incomplete")
        self.assertTrue(result.data["managed_by_environment"])
        self.assertIn("运行环境", "".join(result.suggestions))
        serialized = json.dumps(result.to_dict(), ensure_ascii=False)
        self.assertNotIn("JELLYFIN_API_KEY", serialized)
        self.assertNotIn("jellyfin.internal", serialized)

    def test_ready_component_has_no_blocked_capabilities(self):
        result = _explain_with_values(
            "jellyfin",
            {
                "JELLYFIN_ENABLED": "1",
                "JELLYFIN_URL": "http://jellyfin.internal:8096",
                "JELLYFIN_API_KEY": "secret",
            },
        )
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.summary, "Jellyfin：已就绪")
        self.assertTrue(result.data["enabled"])
        self.assertEqual(result.data["missing_field_labels"], [])
        self.assertEqual(result.data["blocked_capabilities"], [])
        self.assertIn("测试Jellyfin连接", "".join(result.suggestions))

    def test_ai_recognition_reuses_media_agent_provider(self):
        result = _explain_with_values(
            "ai_recognition",
            {
                "AI_RECOGNITION_ENABLED": "1",
                "AGENT_LLM_API_URL": "https://agent.invalid/v1/chat/completions",
                "AGENT_LLM_MODEL": "shared-model",
            },
        )
        self.assertEqual(result.status, "ready")
        self.assertEqual(
            result.data["required_field_labels"], ["Media Agent 模型连接配置"]
        )
        self.assertEqual(result.data["missing_field_labels"], [])

    def test_ai_recognition_points_incomplete_setup_to_media_agent(self):
        result = _explain_with_values("ai_recognition", {"AI_RECOGNITION_ENABLED": "1"})
        self.assertEqual(result.status, "incomplete")
        self.assertEqual(
            result.data["missing_field_labels"], ["Media Agent 模型连接配置"]
        )
        self.assertIn("Media Agent", "".join(result.suggestions))

    def test_disabled_feature_offers_only_confirmed_safe_action(self):
        feature_summary = ToolResult(
            True,
            "attention",
            "feature summary",
            data={
                "features": [
                    {
                        "feature": "douban",
                        "label": "豆瓣探索",
                        "enabled": False,
                        "availability": "disabled",
                        "reason_codes": ["feature_disabled"],
                        "managed_by_environment": False,
                    }
                ]
            },
        )
        with patch(
            "app.agent.config_explain_actions.summarize_feature_states",
            return_value=feature_summary,
        ):
            result = explain_config_component({"component": "douban"})
        self.assertEqual(result.status, "disabled")
        self.assertEqual(
            result.data["agent_action"],
            {
                "supported": True,
                "tool": "config.set_feature_state",
                "feature": "douban",
                "enabled": True,
                "requires_confirmation": True,
                "prompt": "开启豆瓣探索",
            },
        )

    def test_environment_managed_feature_never_offers_agent_write(self):
        feature_summary = ToolResult(
            True,
            "attention",
            "feature summary",
            data={
                "features": [
                    {
                        "feature": "douban",
                        "label": "豆瓣探索",
                        "enabled": True,
                        "availability": "blocked",
                        "reason_codes": ["parent_disabled"],
                        "managed_by_environment": True,
                    }
                ]
            },
        )
        with patch(
            "app.agent.config_explain_actions.summarize_feature_states",
            return_value=feature_summary,
        ):
            result = explain_config_component({"component": "douban"})
        self.assertEqual(result.status, "blocked")
        self.assertTrue(result.data["managed_by_environment"])
        self.assertIsNone(result.data["agent_action"])
        self.assertIn("运行环境", "".join(result.suggestions))


class ConfigDiagnosisConsistencyTests(unittest.TestCase):
    def test_strm_summary_and_explanation_share_native_source_validation(self):
        from app.agent.config_diagnosis_actions import diagnose_config

        cases = [
            ("", "0", "not_configured"), ("[]", "0", "not_configured"),
            ("[]", "1", "incomplete"), ("not-json", "0", "incomplete"),
            ('["0"]', "0", "incomplete"), ('{"id":"source"}', "0", "incomplete"),
            ('["valid","0"]', "0", "incomplete"),
            ('["valid"]', "0", "ready"),
            ('[{"id":"valid","name":"PRIVATE_SOURCE"}]', "1", "ready"),
        ]
        for sources, scheduled, expected in cases:
            values = {"GY_STRM_SOURCE_DIRS": sources, "STRM_SCHEDULE_ENABLED": scheduled,
                      "GY_STRM_BASE_URL": "http://private.invalid", "STRM_ROOT": "/private/strm"}
            with self.subTest(sources=sources, scheduled=scheduled), patch(
                "app.agent.config_explain_actions.config.get", side_effect=_config_get(values)
            ), patch("app.agent.config_explain_actions.config.has_external_override", return_value=False):
                summary = diagnose_config({})
                explanation = explain_config_component({"component": "strm"})
            entry = next(x for x in summary.data["components"] if x["name"] == "strm")
            self.assertEqual(entry["status"], expected)
            self.assertEqual(explanation.status, expected)
            self.assertEqual(entry["enabled"], explanation.data["enabled"])
            self.assertEqual("光鸭源目录" in explanation.data["missing_field_labels"], expected != "ready")
            self.assertFalse(summary.data["network_accessed"])
            self.assertFalse(explanation.data["network_accessed"])
            self.assertNotIn("STRM 失败诊断", explanation.data["blocked_capabilities"])
            self.assertEqual(summary.data["probe_mode"], "configuration_only")
            if expected == "incomplete":
                self.assertFalse(summary.ok)
                self.assertTrue(any(i["severity"] == "error" and i["code"].startswith("strm_") for i in summary.data["issues"]))
            rendered = json.dumps([summary.to_dict(), explanation.to_dict()], ensure_ascii=False)
            for secret in ("private.invalid", "/private/strm", "PRIVATE_SOURCE", "GY_STRM_SOURCE_DIRS"):
                self.assertNotIn(secret, rendered)

    def test_summary_reads_one_configuration_snapshot_and_preserves_disabled_servers(self):
        from app import config
        from app.agent.config_diagnosis_actions import diagnose_config
        from app.modules.strm import parse_strm_sources

        self.assertEqual(parse_strm_sources("[]", require_nonempty=False), ([], ""))
        values = {"JELLYFIN_ENABLED": "0", "JELLYFIN_URL": "http://secret.invalid",
                  "JELLYFIN_API_KEY": "secret-api", "EMBY_ENABLED": "0", "TMDB_API_KEY": "secret-tmdb",
                  "QB_URL": "http://secret.invalid", "QB_API_KEY": "secret-qb", "AI_RECOGNITION_ENABLED": "0"}
        with patch.object(config, "get", side_effect=_config_get(values)), patch.object(
            config, "get_many", wraps=config.get_many
        ) as reads, patch.object(config, "has_external_override", return_value=False):
            summary = diagnose_config({})
        self.assertEqual(reads.call_count, 1)
        states = {x["name"]: x["status"] for x in summary.data["components"]}
        self.assertEqual(states["jellyfin"], "disabled")
        self.assertEqual(states["tmdb"], "ready")
        self.assertEqual(states["qbittorrent"], "ready")
        self.assertFalse(summary.data["network_accessed"])
        self.assertNotIn("secret", json.dumps(summary.to_dict()))

    def test_manual_strm_incomplete_is_an_error_even_without_timer(self):
        from app.agent.config_diagnosis_actions import diagnose_config

        values = {"GY_STRM_SOURCE_DIRS": '["source"]', "STRM_SCHEDULE_ENABLED": "0", "STRM_ROOT": "/private/strm"}
        with patch("app.agent.config_explain_actions.config.get", side_effect=_config_get(values)), patch(
            "app.agent.config_explain_actions.config.has_external_override", return_value=False
        ):
            result = diagnose_config({})
        self.assertEqual(result.status, "attention")
        self.assertTrue(any(i["code"] == "strm_incomplete" for i in result.data["issues"]))
