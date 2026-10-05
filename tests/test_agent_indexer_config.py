"""Media Agent 受控资源站点配置测试。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config
from app.agent.errors import AgentToolError
from app.agent.indexer_config_actions import (
    current_indexer_site_ids,
    indexer_sites_arguments,
    prepare_indexer_sites_confirmation,
    summarize_indexer_sites,
    verify_indexer_sites_write,
)
from app.agent.models import Evidence, RiskLevel, ToolReference, ToolResult
from app.agent.public_view import format_public_result
from app.indexers.config import (
    INDEXER_SITE_CONFIG_VERSION,
    INDEXER_SITE_CONFIG_VERSION_KEY,
    build_indexer_site_updates,
    expand_legacy_indexer_site_ids,
    normalize_indexer_site_ids,
    normalize_persisted_indexer_site_ids,
)
from app.routes.api import _normalize_indexer_sites, get_config
from tests.agent_kernel_test_harness import (
    build_kernel_test_registry as build_tool_registry,
)


class IndexerSiteConfigUnitTests(unittest.TestCase):
    def test_shared_normalizer_is_ordered_deduplicated_and_strict(self):
        self.assertEqual(
            normalize_indexer_site_ids(["tpb", "Nyaa", "sukebei", "tpb"]),
            ("nyaa", "tpb", "sukebei"),
        )
        self.assertEqual(
            build_indexer_site_updates("tpb, nyaa, sukebei, tpb"),
            {
                "INDEXER_ENABLED_SITES": "nyaa,tpb,sukebei",
                INDEXER_SITE_CONFIG_VERSION_KEY: INDEXER_SITE_CONFIG_VERSION,
                "INDEXER_SUKEBEI_ENABLED": "1",
            },
        )
        self.assertEqual(
            normalize_persisted_indexer_site_ids("nyaa,animetosho"), ("nyaa",)
        )
        for value in (["nyaa", "evil"], ["nyaa", 1], "nyaa\nevil", object()):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_indexer_site_ids(value)
        with self.assertRaises(ValueError):
            normalize_indexer_site_ids("nyaa,animetosho")

    def test_retired_source_is_removed_only_from_persisted_configuration(self):
        self.assertEqual(normalize_persisted_indexer_site_ids("nyaa,1lou,btbtla"), ("nyaa", "btbtla"))
        self.assertEqual(normalize_persisted_indexer_site_ids("1lou"), ())
        legacy = ["nyaa", "mikan", "btbtla", "1lou", "tpb", "sukebei"] * 2
        self.assertEqual(normalize_persisted_indexer_site_ids(legacy), ("nyaa", "mikan", "btbtla", "tpb", "sukebei"))
        with self.assertRaises(ValueError):
            normalize_indexer_site_ids("1lou")
        with self.assertRaises(AgentToolError):
            indexer_sites_arguments({"site_ids": ["1lou"]})

    def test_legacy_bundle_expands_only_without_the_explicit_v2_marker(self):
        old_sites = normalize_persisted_indexer_site_ids("nyaa,btbtla,tpb")
        self.assertEqual(
            expand_legacy_indexer_site_ids(old_sites),
            ("nyaa", "btbtla", "aipan", "dygang", "ys5266", "tpb"),
        )
        self.assertEqual(
            expand_legacy_indexer_site_ids(old_sites, format_version="2"),
            ("nyaa", "btbtla", "tpb"),
        )
        self.assertEqual(
            expand_legacy_indexer_site_ids(old_sites, format_version="3"),
            ("nyaa", "btbtla", "tpb"),
        )

    def test_agent_current_site_projection_obeys_the_format_marker(self):
        values = {
            "INDEXER_ENABLED_SITES": "nyaa,btbtla,tpb",
            "INDEXER_SITE_CONFIG_VERSION": "",
        }
        with patch(
            "app.agent.indexer_config_actions.config.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch(
            "app.agent.indexer_config_actions.config.get_bool", return_value=False,
        ):
            self.assertEqual(
                current_indexer_site_ids(),
                ("nyaa", "btbtla", "aipan", "dygang", "ys5266", "tpb"),
            )
            values["INDEXER_SITE_CONFIG_VERSION"] = "2"
            self.assertEqual(
                current_indexer_site_ids(), ("nyaa", "btbtla", "tpb")
            )

    def test_settings_projection_preserves_legacy_enabled_children_until_saved(self):
        persisted = {"INDEXER_ENABLED_SITES": "nyaa,mikan,btbtla,tpb"}
        format_version = [""]

        def get_value(key, default=""):
            if key == "INDEXER_SITE_CONFIG_VERSION":
                return format_version[0]
            return default

        with patch("app.routes.api.require_api_login"), patch(
            "app.routes.api.config.all_items", return_value=persisted,
        ), patch(
            "app.routes.api.config.has_external_override", return_value=False,
        ), patch("app.routes.api.config.get", side_effect=get_value):
            old_settings = get_config(object())
            format_version[0] = "2"
            new_settings = get_config(object())

        self.assertEqual(
            old_settings["INDEXER_ENABLED_SITES"],
            "nyaa,mikan,btbtla,aipan,dygang,ys5266,tpb",
        )
        self.assertEqual(
            new_settings["INDEXER_ENABLED_SITES"], "nyaa,mikan,btbtla,tpb"
        )

    def test_arguments_reject_arbitrary_configuration_and_empty_selection(self):
        self.assertEqual(
            indexer_sites_arguments({"site_ids": ["tpb", "nyaa"]}),
            {"site_ids": ["nyaa", "tpb"]},
        )
        self.assertEqual(
            indexer_sites_arguments(
                {"site_ids": ["tpb", "nyaa"], "enable_search": False}
            ),
            {"site_ids": ["nyaa", "tpb"], "enable_search": False},
        )
        for arguments in (
            {},
            {"site_ids": []},
            {"site_ids": "nyaa,mikan"},
            {"site_ids": ["tpb", "nyaa", "tpb"]},
            {"site_ids": ["nyaa"] * 9},
            {"site_ids": ["evil"]},
            {"site_ids": ["nyaa", 1]},
            {"site_ids": ["nyaa"], "key": "TMDB_API_KEY"},
            {"site_ids": ["nyaa"], "cookie": "secret"},
            {"site_ids": ["nyaa"], "enable_search": 1},
            {"site_ids": ["nyaa"], "enable_search": "true"},
            {"site_ids": ["nyaa"], "enable_search": None},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(AgentToolError):
                indexer_sites_arguments(arguments)

    def test_settings_route_keeps_legacy_string_input_contract(self):
        self.assertEqual(_normalize_indexer_sites(None), "")
        self.assertEqual(_normalize_indexer_sites(0), "")
        self.assertEqual(_normalize_indexer_sites(False), "")
        self.assertEqual(_normalize_indexer_sites("tpb,nyaa,tpb"), "nyaa,tpb")
        with self.assertRaises(ValueError):
            _normalize_indexer_sites(["nyaa", "tpb"])

    def test_registry_exposes_read_and_confirmation_gated_write(self):
        registry = build_tool_registry()
        capabilities = {item["name"]: item for item in registry.capabilities()}
        self.assertEqual(
            capabilities["config.indexer_sites_summary"]["risk"], RiskLevel.READ.value
        )
        self.assertEqual(
            capabilities["config.set_indexer_sites"]["risk"], RiskLevel.LOW_WRITE.value
        )
        self.assertTrue(
            capabilities["config.set_indexer_sites"]["requires_confirmation"]
        )
        with self.assertRaisesRegex(AgentToolError, "需要确认"):
            registry.execute("config.set_indexer_sites", {"site_ids": ["nyaa"]})

    def test_summary_preview_and_context_do_not_leak_other_config(self):
        with tempfile.TemporaryDirectory() as root:
            env_file = Path(root) / "user.env"
            secret = "super-secret-must-not-leak"
            config.write_env_file(
                env_file,
                {
                    "INDEXER_ENABLED_SITES": "mikan,nyaa",
                    "INDEXER_SUKEBEI_ENABLED": "0",
                    "TMDB_API_KEY": secret,
                },
                replace=False,
            )
            with (
                patch.dict(
                    os.environ,
                    {
                        "INDEXER_ENABLED_SITES": "",
                        "INDEXER_SUKEBEI_ENABLED": "",
                        "INDEXER_SEARCH_ENABLED": "",
                    },
                    clear=False,
                ),
                patch.object(config, "ENV_FILE", env_file),
                patch.object(config, "_cache", None),
                patch.object(config, "_STARTUP_ENV_OVERRIDES", frozenset()),
            ):
                summary = summarize_indexer_sites({})
                preview, context = prepare_indexer_sites_confirmation(
                    {"site_ids": ["nyaa", "tpb"]}
                )
            rendered = repr((summary.to_dict(), preview.to_dict(), context))
            self.assertNotIn(secret, rendered)
            self.assertNotIn("TMDB_API_KEY", rendered)
            self.assertNotIn(str(env_file), rendered)
            self.assertEqual(summary.data["site_count"], 2)
            self.assertEqual(
                [site["site_id"] for site in summary.data["sites"]], ["nyaa", "mikan"]
            )
            self.assertTrue(summary.data["search_enabled"])
            self.assertEqual(len(context), 64)

    def test_post_write_verification_preserves_failure_without_readback(self):
        result = ToolResult(
            ok=False,
            status="conflict",
            summary="配置已被其他操作修改，请重新预检",
            data={"receipt": "indexer-receipt", "site_count": 2},
            evidence=[Evidence("test", "original evidence", "now")],
            suggestions=["original suggestion"],
            error="配置已变化。",
            references=[ToolReference("config_receipt", "indexer-receipt")],
            effect_metadata={"receipt_id": "indexer-receipt"},
        )
        with patch.object(config, "read_env_snapshot") as readback:
            checked = verify_indexer_sites_write(
                {"site_ids": ["nyaa", "tpb"], "enable_search": True}, result
            )

        self.assertIs(checked, result)
        readback.assert_not_called()
        self.assertEqual(checked.to_dict(), result.to_dict())

    def test_post_write_verification_reports_pending_as_unknown(self):
        evidence = [Evidence("test", "original evidence", "now")]
        result = ToolResult(
            ok=True,
            status="completed",
            summary="已保存 2 个资源站点",
            data={"receipt": "indexer-receipt", "site_count": 2},
            evidence=evidence,
            references=[ToolReference("config_receipt", "indexer-receipt")],
            effect_metadata={"receipt_id": "indexer-receipt"},
        )
        with patch.object(
            config,
            "read_env_snapshot",
            return_value=(
                b"persisted",
                {
                    "INDEXER_ENABLED_SITES": "nyaa",
                    "INDEXER_SUKEBEI_ENABLED": "0",
                    "INDEXER_SEARCH_ENABLED": "1",
                },
            ),
        ):
            checked = verify_indexer_sites_write(
                {"site_ids": ["nyaa", "tpb"], "enable_search": True}, result
            )

        self.assertFalse(checked.ok)
        self.assertEqual(checked.status, "outcome_unknown")
        self.assertEqual(checked.data["verification_state"], "pending")
        self.assertEqual(checked.data["receipt"], "indexer-receipt")
        self.assertEqual(checked.evidence, evidence)
        self.assertIs(checked.references, result.references)
        self.assertIs(checked.effect_metadata, result.effect_metadata)
        self.assertIn("已提交", checked.summary)
        self.assertIn("待核验", checked.error)
        self.assertIn("请先查看配置而非直接重试", checked.error)
        public = format_public_result(checked.to_dict())
        self.assertTrue(public.startswith("⚠️ "))
        self.assertNotIn("✅", public)
        self.assertIn("请先查看配置而非直接重试", public)

    def test_post_write_verification_keeps_matching_success(self):
        evidence = [Evidence("test", "original evidence", "now")]
        result = ToolResult(
            ok=True,
            status="completed",
            summary="已保存 2 个资源站点",
            data={"receipt": "indexer-receipt", "site_count": 2},
            evidence=evidence,
            references=[ToolReference("config_receipt", "indexer-receipt")],
            effect_metadata={"receipt_id": "indexer-receipt"},
        )
        with patch.object(
            config,
            "read_env_snapshot",
            return_value=(
                b"persisted",
                {
                    "INDEXER_ENABLED_SITES": "nyaa,tpb",
                    "INDEXER_SITE_CONFIG_VERSION": "2",
                    "INDEXER_SUKEBEI_ENABLED": "0",
                    "INDEXER_SEARCH_ENABLED": "1",
                },
            ),
        ):
            checked = verify_indexer_sites_write(
                {"site_ids": ["nyaa", "tpb"], "enable_search": True}, result
            )

        self.assertTrue(checked.ok)
        self.assertEqual(checked.status, "completed")
        self.assertEqual(checked.summary, result.summary)
        self.assertEqual(checked.data["verification_state"], "verified")
        self.assertEqual(checked.data["receipt"], "indexer-receipt")
        self.assertEqual(checked.evidence[:1], evidence)
        self.assertEqual(len(checked.evidence), len(evidence) + 1)
        self.assertIs(checked.references, result.references)
        self.assertIs(checked.effect_metadata, result.effect_metadata)
        self.assertTrue(format_public_result(checked.to_dict()).startswith("✅ "))
