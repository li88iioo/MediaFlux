"""Media Agent 受控资源站点配置测试。"""

from __future__ import annotations
from starlette.requests import Request
from typing import Any, cast

import os
import asyncio
from types import SimpleNamespace

from app.agent.kernel import sandbox
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.ports.sdk_effects import indexer_configuration_tool
from app.agent.kernel.effects import ConfirmationEffectPlanStore
from app.agent.confirmation import SQLiteConfirmationStore
from app.agent.errors import ToolPipelineError
from tests.support import initialized_web_runtime
from tests.test_agent_sdk_effects import context_for, executor
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config
from app.indexers.config import (
    current_indexer_site_ids,
    normalize_site_selection,
    prepare_site_configuration,
    apply_site_configuration,
    site_configuration,
    SiteConfigurationChanged,
)
from app.agent.errors import AgentToolError
from app.agent.models import RiskLevel
from app.indexers.config import (
    INDEXER_SITE_CONFIG_VERSION,
    INDEXER_SITE_CONFIG_VERSION_KEY,
    build_indexer_site_updates,
    normalize_indexer_site_ids,
    normalize_persisted_indexer_site_ids,
)
from app.routes.api import _validate_indexer_updates, get_config
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
        with self.assertRaises((ValueError, TypeError)):
            normalize_site_selection(**{"site_ids": ["1lou"]})

    def test_persisted_agent_only_sites_are_not_manual_options(self):
        self.assertEqual(normalize_persisted_indexer_site_ids("nyaa,btbtla,aipan,dygang,ys5266,tpb"), ("nyaa", "btbtla", "tpb"))
        for site in ("aipan", "dygang", "ys5266"):
            with self.subTest(site=site), self.assertRaises(ValueError):
                normalize_indexer_site_ids([site])

    def test_manual_config_projection_does_not_expand_agent_sources(self):
        values = {
            "INDEXER_ENABLED_SITES": "nyaa,btbtla,tpb",
            "INDEXER_SITE_CONFIG_VERSION": "",
        }
        with patch(
            "app.indexers.config.config.get",
            side_effect=lambda key, default="": values.get(key, default),
        ), patch(
            "app.indexers.config.config.get_bool", return_value=False,
        ):
            self.assertEqual(
                current_indexer_site_ids(),
                ("nyaa", "btbtla", "tpb"),
            )
            values["INDEXER_SITE_CONFIG_VERSION"] = "2"
            self.assertEqual(
                current_indexer_site_ids(), ("nyaa", "btbtla", "tpb")
            )

    def test_settings_projection_removes_agent_sources_from_old_manual_settings(self):
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
            old_settings = get_config(cast(Request, object()))
            format_version[0] = "2"
            new_settings = get_config(cast(Request, object()))

        self.assertEqual(
            old_settings["INDEXER_ENABLED_SITES"],
            "nyaa,mikan,btbtla,tpb",
        )
        self.assertEqual(
            new_settings["INDEXER_ENABLED_SITES"], "nyaa,mikan,btbtla,tpb"
        )

    def test_arguments_reject_arbitrary_configuration_and_empty_selection(self):
        self.assertEqual(
            normalize_site_selection(**{"site_ids": ["tpb", "nyaa"]}),
            {"site_ids": ["nyaa", "tpb"]},
        )
        self.assertEqual(
            normalize_site_selection(**
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
        ):
            with self.subTest(arguments=arguments), self.assertRaises((ValueError, TypeError)):
                normalize_site_selection(**cast(Any, arguments))

    def test_settings_route_keeps_legacy_string_input_contract(self):
        discovery = {"DISCOVERY_RESOURCE_RESULTS_ENABLED": "0"}
        for value, expected in ((None, ""), (0, ""), (False, ""), ("tpb,nyaa,tpb", "nyaa,tpb")):
            updates = _validate_indexer_updates({"INDEXER_ENABLED_SITES": value}, discovery)
            self.assertEqual(updates["INDEXER_ENABLED_SITES"], expected)
        with self.assertRaises(ValueError):
            _validate_indexer_updates({"INDEXER_ENABLED_SITES": ["nyaa", "tpb"]}, discovery)

    def test_registry_exposes_read_and_confirmation_gated_write(self):
        registry = build_tool_registry()
        capabilities = {item["name"]: item for item in registry.capabilities()}
        self.assertNotIn("config.indexer_sites_summary", capabilities)
        self.assertNotIn("config.set_indexer_sites", capabilities)
        self.assertEqual(
            capabilities["resources.configure_sites"]["risk"], RiskLevel.LOW_WRITE.value
        )
        self.assertTrue(
            capabilities["resources.configure_sites"]["requires_confirmation"]
        )
        with self.assertRaisesRegex(AgentToolError, "需要确认"):
            registry.execute("resources.configure_sites", {"site_ids": ["nyaa"]})

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
                summary = site_configuration()
                preview, context = prepare_site_configuration(**
                    {"site_ids": ["nyaa", "tpb"]}
                )
            rendered = repr((summary, preview, context))
            self.assertNotIn(secret, rendered)
            self.assertNotIn("TMDB_API_KEY", rendered)
            self.assertNotIn(str(env_file), rendered)
            self.assertEqual(summary["site_count"], 2)
            self.assertEqual(
                [site["site_id"] for site in summary["sites"]], ["nyaa", "mikan"]
            )
            self.assertTrue(summary["search_enabled"])
            self.assertEqual(len(context), 64)



class SiteConfigurationWriteTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {key: "" for key in (
            "INDEXER_ENABLED_SITES", "INDEXER_SUKEBEI_ENABLED", "INDEXER_SITE_CONFIG_VERSION", "INDEXER_SEARCH_ENABLED")}))
        paths = self.enterContext(initialized_web_runtime())
        self.root = paths.data_dir.parent
        self.env = paths.env_file
        config.update_runtime_env_file(self.env, {"INDEXER_ENABLED_SITES": "nyaa", "INDEXER_SUKEBEI_ENABLED": "0",
            "INDEXER_SEARCH_ENABLED": "1", "AGENT_ENABLED": "1", "UNCHANGED_SETTING": "keep-me"}, expected=config.read_env_snapshot(self.env)[0])
        self.refresh = self.enterContext(patch("app.indexers.runtime.refresh_indexer_configuration",
            return_value={"web": True, "telegram": False}))

    def test_save_readback_preserves_omitted_switch_and_reports_refresh_failure(self):
        args = {"site_ids": ["tpb", "nyaa", "sukebei"]}
        preview, fingerprint = prepare_site_configuration(**args)
        self.assertIn("Sukebei", repr(preview))
        result = apply_site_configuration(**args, expected_fingerprint=fingerprint)
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["verification_state"], "verified")
        self.assertFalse(result["data"]["runtime_refreshed"])
        self.assertTrue(result["suggestions"])
        _, saved = config.read_env_snapshot(self.env)
        self.assertEqual(saved["INDEXER_ENABLED_SITES"], "nyaa,tpb,sukebei")
        self.assertEqual(saved["INDEXER_SEARCH_ENABLED"], "1")
        self.assertEqual(saved["UNCHANGED_SETTING"], "keep-me")
        self.refresh.assert_called_once()

    def test_nochange_and_config_drift_do_not_publish(self):
        _, fingerprint = prepare_site_configuration(site_ids=["nyaa"])
        with patch.object(config, "update_runtime_env_file") as write:
            result = apply_site_configuration(site_ids=["nyaa"], expected_fingerprint=fingerprint)
            self.assertEqual(result["status"], "no_changes")
            write.assert_not_called()
        _, fingerprint = prepare_site_configuration(site_ids=["tpb"])
        config.write_env_file(self.env, {"OTHER_CHANGE": "keep"}, replace=True)
        with self.assertRaises(SiteConfigurationChanged):
            apply_site_configuration(site_ids=["tpb"], expected_fingerprint=fingerprint)
        self.refresh.assert_not_called()

    def test_post_write_mismatch_is_unknown_and_deployment_override_is_read_only(self):
        args = {"site_ids": ["tpb"], "enable_search": False}
        _, fingerprint = prepare_site_configuration(**args)
        with patch.object(config, "update_runtime_env_file"):
            result = apply_site_configuration(**args, expected_fingerprint=fingerprint)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "outcome_unknown")
        self.assertEqual(result["data"]["verification_state"], "pending")
        with patch.object(config, "has_external_override", return_value=True):
            result, fingerprint = prepare_site_configuration(**args)
            self.assertEqual(result["status"], "environment_override")
            with patch.object(config, "update_runtime_env_file") as write:
                self.assertEqual(apply_site_configuration(**args, expected_fingerprint=fingerprint)["status"], "environment_override")
                write.assert_not_called()


    def test_cold_confirmation_cancel_and_lost_receipt_do_not_replay(self):
        async def check():
            for mode in ("cancel", "unknown", "commit"):
                context = await context_for(SQLiteKernelStore(), owner="webk:v1:indexer-test", session="indexer-" + mode)
                effects = executor(SQLiteKernelStore())
                prepared = await effects.prepare("resources.configure_sites", {"site_ids": ["tpb"]}, context=context)
                plan = prepared.effect_plan
                assert plan is not None
                if mode == "cancel":
                    assert await effects.cancel_effect(plan.plan_id, lease=context.lease)
                    with self.assertRaises(ToolPipelineError):
                        await executor(SQLiteKernelStore()).execute_confirmed(plan.plan_id, context=context)
                    self.refresh.assert_not_called()
                elif mode == "unknown":
                    with patch.object(config, "update_runtime_env_file", side_effect=config.AtomicPublishError("lost")) as write:
                        result = await executor(SQLiteKernelStore()).execute_confirmed(plan.plan_id, context=context)
                        assert result.outcome.public_content["status"] == "outcome_unknown"
                        with self.assertRaises(ToolPipelineError):
                            await executor(SQLiteKernelStore()).execute_confirmed(plan.plan_id, context=context)
                        assert write.call_count == 1
                else:
                    from concurrent.futures import ThreadPoolExecutor
                    def refreshed():
                        def read_config():
                            with config.runtime_config_read_guard():
                                return config.capture_runtime_config_snapshot()
                        with ThreadPoolExecutor(1) as pool:
                            pool.submit(read_config).result(timeout=2)
                        return {"web": True, "telegram": True}
                    self.refresh.side_effect = refreshed
                    result = await executor(SQLiteKernelStore()).execute_confirmed(plan.plan_id, context=context)
                    assert result.outcome.public_content["data"]["verification_state"] == "verified"
                    assert current_indexer_site_ids() == ("tpb",)
                    with self.assertRaises(ToolPipelineError):
                        await executor(SQLiteKernelStore()).execute_confirmed(plan.plan_id, context=context)
                    self.refresh.assert_called_once()
        asyncio.run(check())

    @unittest.skipUnless(sandbox.sandbox_supported(), "真实Linux root工作区容器验收")
    def test_workspace_python_read_and_cli_proposal_use_shared_configuration(self):
        self.enterContext(patch.object(sandbox, "get_runtime_paths", return_value=SimpleNamespace(
            data_dir=self.root, runtime_dir=self.root / "runtime")))
        async def check():
            store = SQLiteKernelStore()
            context = await context_for(store)
            catalog = ToolCatalog([indexer_configuration_tool()])
            pipeline = ToolPipeline(catalog=catalog, state_store=store, reference_store=store,
                effect_store=ConfirmationEffectPlanStore(SQLiteConfirmationStore()))
            catalog.register(sandbox.run_code_tool(pipeline))
            try:
                result = await pipeline.execute(sandbox.RUN_CODE_TOOL, {"code": 'from mf import resources; print(resources.sources()["configuration"]["site_ids"])'}, context=context)
                assert result.outcome.public_content["data"]["exit_code"] == 0
                assert "nyaa" in result.outcome.public_content["data"]["stdout"]
                proposed = await pipeline.execute(sandbox.RUN_CODE_TOOL, {"code": "mf resources.configure_sites --site-ids nyaa tpb", "language": "shell"}, context=context)
                assert proposed.effect_plan is not None, proposed.outcome.public_content
                assert current_indexer_site_ids() == ("nyaa",)
                self.refresh.assert_not_called()
                await executor(SQLiteKernelStore()).execute_confirmed(proposed.effect_plan.plan_id, context=context)
                assert current_indexer_site_ids() == ("nyaa", "tpb")
            finally:
                sandbox.clear_workspace(owner=context.owner, session_id=context.session_id)
        asyncio.run(check())
