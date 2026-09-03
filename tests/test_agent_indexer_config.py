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
    indexer_sites_arguments,
    prepare_indexer_sites_confirmation,
    summarize_indexer_sites,
)
from app.agent.models import RiskLevel
from app.indexers.config import (
    build_indexer_site_updates,
    normalize_indexer_site_ids,
    normalize_persisted_indexer_site_ids,
)
from app.routes.api import _normalize_indexer_sites
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
