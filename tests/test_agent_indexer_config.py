"""Media Agent 受控资源站点配置测试。"""
from __future__ import annotations

import asyncio
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import config
from app.agent.indexer_config_actions import (
    indexer_sites_arguments,
    prepare_indexer_sites_confirmation,
    summarize_indexer_sites,
)
from app.agent.models import RiskLevel, ToolResult
from app.agent.orchestrator import (
    AgentOrchestrator,
    indexer_site_change_followup_request,
    indexer_site_change_request,
    indexer_sites_request,
    is_indexer_sites_summary_message,
    resolve_indexer_site_change,
)
from app.agent.rate_limit import agent_rate_limiter
from app.agent.registry import AgentToolError
from app.agent.service import reset_agent_service_for_tests
from app.agent.tools import build_tool_registry
from app.indexers.config import (
    build_indexer_site_updates,
    DEFAULT_INDEXER_SITE_IDS,
    INDEXER_SITE_ORDER,
    normalize_indexer_site_ids,
    normalize_persisted_indexer_site_ids,
)
from app.main import create_app
from app.routes.api import _normalize_indexer_sites
from tests.support import IsolatedDatabaseTestCase


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
            normalize_persisted_indexer_site_ids("nyaa,animetosho"),
            ("nyaa",),
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
            indexer_sites_arguments({
                "site_ids": ["tpb", "nyaa"],
                "enable_search": False,
            }),
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

    def test_natural_language_requires_explicit_single_action(self):
        self.assertEqual(
            indexer_sites_request("将资源站点设为 Nyaa、Mikan、TPB"),
            {"site_ids": ["nyaa", "mikan", "tpb"]},
        )
        self.assertEqual(
            indexer_sites_request("把参与资源检索的站点设置为 Sukebei 和 TPB"),
            {"site_ids": ["sukebei", "tpb"]},
        )
        with self.assertRaises(AgentToolError):
            indexer_sites_request("把参与资源检索的站点设置为 AnimeTosho")
        self.assertEqual(
            indexer_sites_request("只启用蜜柑和海盗湾这些资源站点"),
            {"site_ids": ["mikan", "tpb"]},
        )
        self.assertEqual(
            indexer_sites_request("设置资源站点为 Nyaa"),
            {"site_ids": ["nyaa"]},
        )
        self.assertEqual(
            indexer_sites_request("把资源站点改成 Nyaa"),
            {"site_ids": ["nyaa"]},
        )
        for message in (
            "当前启用了哪些资源站点",
            "不要把资源站点设为 Nyaa",
            "如果资源站点设为 Nyaa 会怎样",
            "能否把资源站点设为 Nyaa",
            "把资源站点设为 Nyaa 然后关闭探索",
            "搜索资源站 Nyaa 的资源",
        ):
            with self.subTest(message=message):
                self.assertIsNone(indexer_sites_request(message))
        with self.assertRaisesRegex(AgentToolError, "未知"):
            indexer_sites_request("资源站点设为 unknown-provider")

    def test_incremental_and_safe_all_site_commands_resolve_to_full_targets(self):
        self.assertEqual(
            indexer_site_change_request("启用所有普通资源站点"),
            {"operation": "replace", "site_ids": list(DEFAULT_INDEXER_SITE_IDS)},
        )
        self.assertEqual(
            indexer_site_change_request("启用所有资源站点，包括 Sukebei"),
            {"operation": "replace", "site_ids": list(INDEXER_SITE_ORDER)},
        )
        self.assertEqual(
            indexer_site_change_request("启用所有普通资源站点，包括Sukebei"),
            {"operation": "clarify_scope_conflict", "site_ids": []},
        )
        self.assertEqual(
            resolve_indexer_site_change(
                indexer_site_change_request("关闭所有资源站点但保留 Nyaa 和 TPB"),
                current_site_ids=["nyaa", "mikan", "tpb"],
            ),
            ["nyaa", "tpb"],
        )
        self.assertEqual(
            resolve_indexer_site_change(
                indexer_site_change_request("关闭除 Nyaa、TPB 外的所有站点"),
                current_site_ids=["nyaa", "mikan", "tpb"],
            ),
            ["nyaa", "tpb"],
        )
        self.assertEqual(
            resolve_indexer_site_change(
                indexer_site_change_request("只启用 Nyaa 站点"),
                current_site_ids=["nyaa", "mikan", "tpb"],
            ),
            ["nyaa"],
        )
        self.assertEqual(
            indexer_site_change_request("把所有站点都打开"),
            {
                "operation": "replace",
                "site_ids": list(DEFAULT_INDEXER_SITE_IDS),
            },
        )
        for message in (
            "开启未打开的资源站点",
            "开大未启开的所有资源站点",
        ):
            with self.subTest(message=message):
                unopened = indexer_site_change_request(message)
                self.assertEqual(
                    unopened,
                    {"operation": "add", "site_ids": list(DEFAULT_INDEXER_SITE_IDS)},
                )
                self.assertEqual(
                    resolve_indexer_site_change(
                        unopened, current_site_ids=["sukebei"]
                    ),
                    list(normalize_indexer_site_ids((*DEFAULT_INDEXER_SITE_IDS, "sukebei"))),
                )
        self.assertEqual(
            indexer_site_change_request("关闭所有站点"),
            {"operation": "replace", "site_ids": []},
        )
        self.assertEqual(
            resolve_indexer_site_change(
                indexer_site_change_request("添加 Mikan 和 TPB 站点"),
                current_site_ids=["nyaa", "1lou"],
            ),
            ["nyaa", "mikan", "1lou", "tpb"],
        )
        self.assertEqual(
            resolve_indexer_site_change(
                indexer_site_change_request("把 1LOU 关闭"),
                current_site_ids=["nyaa", "1lou"],
            ),
            ["nyaa"],
        )

    def test_compound_site_change_then_search_keeps_title_and_safe_scope(self):
        self.assertEqual(
            indexer_site_change_followup_request(
                "开启所有索引站点之后继续搜索《师兄啊师兄》的资源", []
            ),
            {
                "change": {
                    "operation": "replace",
                    "site_ids": list(DEFAULT_INDEXER_SITE_IDS),
                },
                "title": "师兄啊师兄",
            },
        )
        self.assertEqual(
            indexer_site_change_followup_request(
                "开启所有索引站点之后继续搜《师兄啊师兄》的资源", []
            ),
            {
                "change": {
                    "operation": "replace",
                    "site_ids": list(DEFAULT_INDEXER_SITE_IDS),
                },
                "title": "师兄啊师兄",
            },
        )
        self.assertEqual(
            indexer_site_change_followup_request(
                "开启所有普通资源站点之后继续搜索",
                [{"role": "user", "text": "搜索一下师兄啊师兄资源"}],
            )["title"],
            "师兄啊师兄",
        )
        self.assertEqual(
            indexer_site_change_followup_request(
                "开启所有站点继续搜索《师兄啊师兄》的资源", []
            )["title"],
            "师兄啊师兄",
        )
        self.assertEqual(
            indexer_site_change_followup_request(
                "开启所有索引站点后继续搜索上一片名",
                [{"role": "user", "text": "搜索一下师兄啊师兄资源"}],
            )["title"],
            "师兄啊师兄",
        )

    def test_summary_intent_is_narrow(self):
        for message in (
            "当前启用了哪些资源站点",
            "查看资源站点配置",
            "现在使用的资源站点有哪些",
            "列出所有索引站点",
            "显示全部资源站点",
        ):
            with self.subTest(message=message):
                self.assertTrue(is_indexer_sites_summary_message(message))
        self.assertFalse(is_indexer_sites_summary_message("搜索 Nyaa 上的沙丘"))
        self.assertFalse(is_indexer_sites_summary_message("将资源站点设为 Nyaa"))

    def test_settings_route_keeps_legacy_string_input_contract(self):
        self.assertEqual(_normalize_indexer_sites(None), "")
        self.assertEqual(_normalize_indexer_sites(0), "")
        self.assertEqual(_normalize_indexer_sites(False), "")
        self.assertEqual(_normalize_indexer_sites("tpb,nyaa,tpb"), "nyaa,tpb")
        with self.assertRaises(ValueError):
            _normalize_indexer_sites(["nyaa", "tpb"])

    def test_query_route_priority_and_owner_requirement(self):
        agent = AgentOrchestrator(build_tool_registry())
        feature = agent.query("关闭多站资源搜索", owner="owner")
        self.assertEqual(feature["tool_call"]["name"], "config.set_feature_state")
        readiness = agent.query("检查资源站状态")
        self.assertEqual(readiness["tool_call"]["name"], "indexer.diagnose_readiness")
        generic_config = agent.query("检查资源站配置")
        self.assertEqual(generic_config["tool_call"]["name"], "config.diagnose")
        summary = agent.query("查看资源站点配置")
        self.assertEqual(summary["tool_call"]["name"], "config.indexer_sites_summary")
        ownerless = agent.query("设置资源站点为 Nyaa")
        self.assertEqual(ownerless["result"]["status"], "unsupported")
        self.assertIn("已登录会话", ownerless["result"]["summary"])

        vague = agent.query("开启多个站点进行搜索", owner="owner")
        self.assertEqual(vague["mode"], "clarification")
        self.assertIsNone(vague["tool_call"])
        self.assertIn("Sukebei", vague["result"]["summary"])
        self.assertNotIn("library.search", str(vague))

        all_sites = agent.query("把所有站点都打开", owner="owner")
        self.assertEqual(all_sites["mode"], "confirmation_required")
        self.assertEqual(all_sites["tool_call"]["name"], "config.set_indexer_sites")
        self.assertEqual(
            [
                site["site_id"]
                for site in all_sites["result"]["data"]["requested_sites"]
            ],
            list(DEFAULT_INDEXER_SITE_IDS),
        )
        self.assertTrue(all_sites["result"]["data"]["requested_enable_search"])

        adult_sites = agent.query("把所有站点包括成人站点都打开", owner="owner")
        self.assertEqual(adult_sites["mode"], "confirmation_required")
        self.assertEqual(
            [
                site["site_id"]
                for site in adult_sites["result"]["data"]["requested_sites"]
            ],
            list(INDEXER_SITE_ORDER),
        )

        conflicting_scope = agent.query(
            "启用所有普通资源站点，包括 Sukebei", owner="owner"
        )
        self.assertEqual(conflicting_scope["mode"], "clarification")
        self.assertIsNone(conflicting_scope["tool_call"])
        self.assertIn("普通资源站点", conflicting_scope["result"]["summary"])
        self.assertIn("Sukebei", conflicting_scope["result"]["summary"])

        close_all = agent.query("关闭所有站点", owner="owner")
        self.assertEqual(close_all["mode"], "clarification")
        self.assertIn("关闭多站资源索引", close_all["result"]["summary"])
        self.assertIn("关闭多站资源索引", close_all["result"]["suggestions"])

        with patch(
            "app.agent.orchestrator.current_indexer_site_ids",
            return_value=("nyaa",),
        ), patch(
            "app.agent.indexer_config_actions.current_indexer_site_ids",
            return_value=("nyaa",),
        ):
            add_site = agent.query("添加 Mikan 站点", owner="owner")
            ordinary_sites = agent.query("启用所有普通资源站点", owner="owner")
            ordinary_sites_short = agent.query("把普通站点都打开", owner="owner")
        self.assertEqual(add_site["mode"], "confirmation_required")
        self.assertEqual(add_site["tool_call"]["name"], "config.set_indexer_sites")
        self.assertEqual(
            [site["site_id"] for site in add_site["result"]["data"]["requested_sites"]],
            ["nyaa", "mikan"],
        )

        self.assertEqual(ordinary_sites["mode"], "confirmation_required")
        self.assertEqual(
            [
                site["site_id"]
                for site in ordinary_sites["result"]["data"]["requested_sites"]
            ],
            list(DEFAULT_INDEXER_SITE_IDS),
        )
        self.assertEqual(ordinary_sites_short["mode"], "confirmation_required")
        self.assertEqual(
            [
                site["site_id"]
                for site in ordinary_sites_short["result"]["data"]["requested_sites"]
            ],
            list(DEFAULT_INDEXER_SITE_IDS),
        )

    def test_registry_exposes_read_and_confirmation_gated_write(self):
        registry = build_tool_registry()
        capabilities = {item["name"]: item for item in registry.capabilities()}
        self.assertEqual(
            capabilities["config.indexer_sites_summary"]["risk"],
            RiskLevel.READ.value,
        )
        self.assertEqual(
            capabilities["config.set_indexer_sites"]["risk"],
            RiskLevel.LOW_WRITE.value,
        )
        self.assertTrue(capabilities["config.set_indexer_sites"]["requires_confirmation"])
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
            with patch.dict(
                os.environ,
                {
                    "INDEXER_ENABLED_SITES": "",
                    "INDEXER_SUKEBEI_ENABLED": "",
                    "INDEXER_SEARCH_ENABLED": "",
                },
                clear=False,
            ), patch.object(config, "ENV_FILE", env_file), patch.object(
                config, "_cache", None
            ), patch.object(config, "_STARTUP_ENV_OVERRIDES", frozenset()):
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
                [site["site_id"] for site in summary.data["sites"]],
                ["nyaa", "mikan"],
            )
            self.assertFalse(summary.data["search_enabled"])
            self.assertEqual(len(context), 64)


class IndexerSiteConfigApiTests(IsolatedDatabaseTestCase):
    def setUp(self):
        reset_agent_service_for_tests()
        agent_rate_limiter.reset()
        self.temp = tempfile.TemporaryDirectory()
        self.env_file = Path(self.temp.name) / "user.env"
        self.keys = {
            "INDEXER_ENABLED_SITES",
            "INDEXER_SUKEBEI_ENABLED",
            "INDEXER_SEARCH_ENABLED",
            "DISCOVERY_RESOURCE_RESULTS_ENABLED",
        }
        self.previous_env = {key: os.environ.get(key) for key in self.keys}
        for key in self.keys:
            os.environ.pop(key, None)
        config.write_env_file(
            self.env_file,
            {
                "INDEXER_ENABLED_SITES": "nyaa,mikan",
                "INDEXER_SUKEBEI_ENABLED": "0",
                "INDEXER_SEARCH_ENABLED": "1",
                "DISCOVERY_RESOURCE_RESULTS_ENABLED": "1",
                "TMDB_API_KEY": "must-not-leak",
            },
            replace=False,
        )
        self.env_patch = patch.object(config, "ENV_FILE", self.env_file)
        self.cache_patch = patch.object(config, "_cache", None)
        self.override_patch = patch.object(config, "_STARTUP_ENV_OVERRIDES", frozenset())
        self.env_patch.start()
        self.cache_patch.start()
        self.override_patch.start()
        self.client = TestClient(create_app(start_background=False), raise_server_exceptions=False)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.override_patch.stop()
        self.cache_patch.stop()
        self.env_patch.stop()
        for key in self.keys:
            os.environ.pop(key, None)
        for key, value in self.previous_env.items():
            if value is not None:
                os.environ[key] = value
        self.temp.cleanup()
        reset_agent_service_for_tests()
        agent_rate_limiter.reset()

    @staticmethod
    def _token(html: str) -> str:
        match = re.search(r'name="csrf_token"\s+(?:value|content)="([^"]+)"', html)
        if not match:
            match = re.search(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', html)
        if not match:
            raise AssertionError("CSRF token missing")
        return match.group(1)

    def login(self) -> str:
        token = self._token(self.client.get("/login").text)
        response = self.client.post(
            "/login",
            data={"username": "admin", "password": "123456", "csrf_token": token},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302, response.text)
        return self._token(self.client.get("/settings").text)

    def prepare(self, csrf: str, site_ids: list[str]):
        response = self.client.post(
            "/api/agent/actions/config.set_indexer_sites/prepare",
            headers={"X-CSRF-Token": csrf},
            json={"session_id": "test_session_identifier_0001", "arguments": {"site_ids": site_ids}},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response

    def query_and_confirm(self, csrf: str, message: str):
        agent_rate_limiter.reset()
        headers = {"X-CSRF-Token": csrf}
        prepared = self.client.post(
            "/api/agent/query",
            headers=headers,
            json={"session_id": "test_session_identifier_0001", "message": message},
        )
        self.assertEqual(prepared.status_code, 200, prepared.text)
        body = prepared.json()
        self.assertEqual(body["mode"], "confirmation_required")
        self.assertEqual(body["tool_call"]["name"], "config.set_indexer_sites")
        with patch("app.indexers.runtime.shutdown_indexer_service"), patch(
            "app.modules.telegram_resource_search.shutdown_telegram_indexer_worker"
        ):
            confirmed = self.client.post(
                "/api/agent/actions/confirm",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "plan_id": body["action_plan"]["plan_id"]},
            )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        result = confirmed.json()["result"]
        self.assertTrue(result["ok"])
        return result

    def test_query_prepare_confirm_summary_replay_and_runtime_refresh(self):
        csrf = self.login()
        headers = {"X-CSRF-Token": csrf}
        prepared = self.client.post(
            "/api/agent/query",
            headers=headers,
            json={"session_id": "test_session_identifier_0001", "message": "将资源站点设为 Nyaa、TPB、Sukebei"},
        )
        self.assertEqual(prepared.status_code, 200, prepared.text)
        body = prepared.json()
        self.assertEqual(body["mode"], "confirmation_required")
        self.assertEqual(body["tool_call"]["name"], "config.set_indexer_sites")
        self.assertEqual(body["result"]["data"]["requested_count"], 3)
        before = config._read_env_file(self.env_file)
        self.assertEqual(before["INDEXER_ENABLED_SITES"], "nyaa,mikan")

        confirmation_id = body["action_plan"]["plan_id"]
        with patch("app.indexers.runtime.shutdown_indexer_service") as shutdown, patch(
            "app.modules.telegram_resource_search.shutdown_telegram_indexer_worker"
        ) as shutdown_telegram:
            confirmed = self.client.post(
                "/api/agent/actions/confirm",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "plan_id": confirmation_id},
            )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        result = confirmed.json()["result"]
        self.assertTrue(result["ok"])
        self.assertTrue(result["data"]["runtime_refreshed"])
        self.assertEqual(result["data"]["verification_state"], "verified")
        shutdown.assert_awaited_once()
        shutdown_telegram.assert_called_once_with(timeout=5.0)
        saved = config._read_env_file(self.env_file)
        self.assertEqual(saved["INDEXER_ENABLED_SITES"], "nyaa,tpb,sukebei")
        self.assertEqual(saved["INDEXER_SUKEBEI_ENABLED"], "1")

        summary = self.client.post(
            "/api/agent/query",
            headers=headers,
            json={"session_id": "test_session_identifier_0001", "message": "当前启用了哪些资源站点"},
        )
        self.assertEqual(summary.status_code, 200, summary.text)
        self.assertEqual(summary.json()["tool_call"]["name"], "config.indexer_sites_summary")
        self.assertEqual(summary.json()["result"]["data"]["site_count"], 3)

        replay = self.client.post(
            "/api/agent/actions/confirm",
            headers=headers,
            json={"session_id": "test_session_identifier_0001", "plan_id": confirmation_id},
        )
        self.assertEqual(replay.status_code, 409, replay.text)
        self.assertNotIn("must-not-leak", confirmed.text + summary.text + replay.text)

    def test_natural_language_enable_search_select_second_qb_and_confirm(self):
        csrf = self.login()
        headers = {"X-CSRF-Token": csrf}
        session_id = "indexerJourneySession001"
        first_id = "safe-indexer-result-0001"
        second_id = "safe-indexer-result-0002"
        searched = ToolResult(
            ok=True,
            status="success",
            summary="已找到《光阴之外》的 2 项资源。",
            data={
                "query": "光阴之外",
                "count": 2,
                "items": [
                    {
                        "result_id": first_id,
                        "site_id": "nyaa",
                        "site_name": "Nyaa",
                        "title": "光阴之外 S01E01 1080p",
                        "size_text": "1.2 GB",
                        "download_state": "ready",
                        "download_kinds": ["magnet"],
                    },
                    {
                        "result_id": second_id,
                        "site_id": "mikan",
                        "site_name": "Mikan",
                        "title": "光阴之外 S01E02 1080p",
                        "size_text": "1.3 GB",
                        "download_state": "ready",
                        "download_kinds": ["magnet"],
                    },
                ],
            },
        )
        preview = ToolResult(
            ok=True,
            status="confirmation_required",
            summary="将把第 2 项资源提交到 qBittorrent。",
            data={"target": "qb", "candidate": 2},
        )
        accepted = ToolResult(
            ok=True,
            status="accepted",
            summary="资源已提交到 qBittorrent。",
            data={"target": "qb", "accepted": True},
        )

        with patch("app.agent.tools.search_resources", return_value=searched) as search, patch(
            "app.agent.indexer_candidate_actions.prepare_submit_resource",
            return_value=(preview, "resource:test:second:qb"),
        ) as preview_submit, patch(
            "app.agent.indexer_candidate_actions.submit_resource_confirmed", return_value=accepted
        ) as submit, patch(
            "app.agent.orchestrator.compose_tool_answer", return_value=None
        ), patch(
            "app.indexers.runtime.shutdown_indexer_service"
        ) as shutdown, patch(
            "app.modules.telegram_resource_search.shutdown_telegram_indexer_worker"
        ) as shutdown_telegram:
            # 注册表必须在处理函数被替换后创建，否则会持有原函数引用。
            reset_agent_service_for_tests()

            enable = self.client.post(
                "/api/agent/query",
                headers=headers,
                json={"message": "开启所有资源站点", "session_id": session_id},
            )
            self.assertEqual(enable.status_code, 200, enable.text)
            enable_body = enable.json()
            self.assertEqual(enable_body["mode"], "confirmation_required")
            self.assertEqual(
                enable_body["tool_call"]["name"], "config.set_indexer_sites"
            )
            self.assertEqual(
                [site["site_id"] for site in enable_body["result"]["data"]["requested_sites"]],
                list(DEFAULT_INDEXER_SITE_IDS),
            )
            self.assertEqual(
                config._read_env_file(self.env_file)["INDEXER_ENABLED_SITES"],
                "nyaa,mikan",
            )

            enabled = self.client.post(
                "/api/agent/actions/confirm",
                headers=headers,
                json={
                    "plan_id": enable_body["action_plan"]["plan_id"],
                    "session_id": session_id,
                },
            )
            self.assertEqual(enabled.status_code, 200, enabled.text)

            resource_search = self.client.post(
                "/api/agent/query",
                headers=headers,
                json={"message": "搜索《光阴之外》资源", "session_id": session_id},
            )
            self.assertEqual(resource_search.status_code, 200, resource_search.text)
            self.assertEqual(
                resource_search.json()["tool_call"]["name"], "indexer.search_resources"
            )

            selected = self.client.post(
                "/api/agent/query",
                headers=headers,
                json={"message": "第二个下到 qB", "session_id": session_id},
            )
            self.assertEqual(selected.status_code, 200, selected.text)
            selected_body = selected.json()
            self.assertEqual(selected_body["mode"], "confirmation_required")
            self.assertEqual(
                selected_body["tool_call"]["name"], "ingest.submit"
            )
            preview_submit.assert_called_once_with(
                {"result_id": second_id, "target": "qb"}
            )

            confirmed = self.client.post(
                "/api/agent/actions/confirm",
                headers=headers,
                json={
                    "plan_id": selected_body["action_plan"]["plan_id"],
                    "session_id": session_id,
                },
            )

        self.assertEqual(confirmed.status_code, 202, confirmed.text)
        submit.assert_called_once_with(
            {"result_id": second_id, "target": "qb"},
            "resource:test:second:qb",
        )
        search.assert_called_once_with({
            "title": "光阴之外",
            "original_title": "",
            "english_title": "",
            "aliases": [],
            "year": None,
            "media_type": "",
            "page": 1,
            "sites": [],
            "limit": 20,
        })
        shutdown.assert_awaited_once()
        shutdown_telegram.assert_called_once_with(timeout=5.0)
        values = config._read_env_file(self.env_file)
        self.assertEqual(values["INDEXER_ENABLED_SITES"], ",".join(DEFAULT_INDEXER_SITE_IDS))
        self.assertEqual(values["INDEXER_SUKEBEI_ENABLED"], "0")

    def test_compound_site_enable_confirmation_continues_resource_search(self):
        csrf = self.login()
        headers = {"X-CSRF-Token": csrf}
        searched = ToolResult(
            ok=True,
            status="success",
            summary="已找到《师兄啊师兄》的 2 项资源。",
            data={"query": "师兄啊师兄", "count": 2, "items": []},
        )
        with patch("app.agent.tools.search_resources", return_value=searched) as search, patch(
            "app.indexers.runtime.shutdown_indexer_service"
        ) as shutdown, patch(
            "app.modules.telegram_resource_search.shutdown_telegram_indexer_worker"
        ) as shutdown_telegram:
            prepared = self.client.post(
                "/api/agent/query",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "message": "开启所有索引站点后继续搜索《师兄啊师兄》的资源"},
            )
            self.assertEqual(prepared.status_code, 200, prepared.text)
            body = prepared.json()
            self.assertEqual(body["mode"], "confirmation_required")
            self.assertEqual(body["tool_call"]["name"], "config.set_indexer_sites")
            self.assertEqual(
                [site["site_id"] for site in body["result"]["data"]["requested_sites"]],
                list(DEFAULT_INDEXER_SITE_IDS),
            )
            self.assertEqual(
                config._read_env_file(self.env_file)["INDEXER_ENABLED_SITES"],
                "nyaa,mikan",
            )

            confirmed = self.client.post(
                "/api/agent/actions/confirm",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "plan_id": body["action_plan"]["plan_id"]},
            )

        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        confirmed_body = confirmed.json()
        self.assertEqual(confirmed_body["mode"], "confirmed_action")
        self.assertEqual(confirmed_body["tool_call"]["name"], "indexer.search_resources")
        self.assertEqual(confirmed_body["response_contract"], {
            "task_kind": "action",
            "presentation": "resource_candidates",
            "resource_candidates": "primary",
        })
        self.assertEqual(confirmed_body["result"]["data"]["query"], "师兄啊师兄")
        self.assertIn("已开启多站资源索引", confirmed_body["result"]["summary"])
        self.assertIn("已找到《师兄啊师兄》的 2 项资源", confirmed_body["result"]["summary"])
        search.assert_called_once_with({
            "title": "师兄啊师兄",
            "original_title": "",
            "english_title": "",
            "aliases": [],
            "year": None,
            "media_type": "",
            "page": 1,
            "sites": [],
            "limit": 20,
        })
        shutdown.assert_awaited_once()
        shutdown_telegram.assert_called_once_with(timeout=5.0)
        values = config._read_env_file(self.env_file)
        self.assertEqual(values["INDEXER_ENABLED_SITES"], ",".join(DEFAULT_INDEXER_SITE_IDS))
        self.assertEqual(values["INDEXER_SUKEBEI_ENABLED"], "0")

    def test_compound_site_enable_does_not_search_when_web_runtime_refresh_fails(self):
        csrf = self.login()
        headers = {"X-CSRF-Token": csrf}
        with patch("app.agent.tools.search_resources") as search, patch(
            "app.indexers.runtime.shutdown_indexer_service",
            side_effect=RuntimeError("runtime refresh failed"),
        ) as shutdown, patch(
            "app.modules.telegram_resource_search.shutdown_telegram_indexer_worker"
        ) as shutdown_telegram:
            prepared = self.client.post(
                "/api/agent/query",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "message": "开启所有索引站点后继续搜索《师兄啊师兄》的资源"},
            )
            self.assertEqual(prepared.status_code, 200, prepared.text)
            body = prepared.json()
            self.assertEqual(body["mode"], "confirmation_required")

            confirmed = self.client.post(
                "/api/agent/actions/confirm",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "plan_id": body["action_plan"]["plan_id"]},
            )

        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        confirmed_body = confirmed.json()
        self.assertEqual(confirmed_body["mode"], "confirmed_action")
        self.assertEqual(
            confirmed_body["tool_call"]["name"], "config.set_indexer_sites"
        )
        self.assertTrue(confirmed_body["result"]["ok"])
        self.assertFalse(
            confirmed_body["result"]["data"]["runtime_refresh"]["web"]
        )
        self.assertIn("重启", confirmed.text)
        self.assertIn("师兄啊师兄", confirmed.text)
        search.assert_not_called()
        shutdown.assert_awaited_once()
        shutdown_telegram.assert_called_once_with(timeout=5.0)

    def test_incremental_safe_all_and_explicit_sensitive_site_commands_persist(self):
        csrf = self.login()

        self.query_and_confirm(csrf, "添加 TPB 站点")
        values = config._read_env_file(self.env_file)
        self.assertEqual(values["INDEXER_ENABLED_SITES"], "nyaa,mikan,tpb")
        self.assertEqual(values["INDEXER_SUKEBEI_ENABLED"], "0")

        self.query_and_confirm(csrf, "关闭 Mikan 站点")
        values = config._read_env_file(self.env_file)
        self.assertEqual(values["INDEXER_ENABLED_SITES"], "nyaa,tpb")
        self.assertEqual(values["INDEXER_SUKEBEI_ENABLED"], "0")

        self.query_and_confirm(csrf, "启用所有普通资源站点")
        values = config._read_env_file(self.env_file)
        self.assertEqual(
            values["INDEXER_ENABLED_SITES"],
            ",".join(DEFAULT_INDEXER_SITE_IDS),
        )
        self.assertEqual(values["INDEXER_SUKEBEI_ENABLED"], "0")

        self.query_and_confirm(csrf, "启用所有资源站点，包括 Sukebei")
        values = config._read_env_file(self.env_file)
        self.assertEqual(values["INDEXER_ENABLED_SITES"], ",".join(INDEXER_SITE_ORDER))
        self.assertEqual(values["INDEXER_SUKEBEI_ENABLED"], "1")

    def test_direct_execution_invalid_arguments_noop_and_external_override_fail_closed(self):
        csrf = self.login()
        headers = {"X-CSRF-Token": csrf}
        direct = self.client.post(
            "/api/agent/tools/config.set_indexer_sites",
            headers=headers,
            json={"session_id": "test_session_identifier_0001", "arguments": {"site_ids": ["nyaa"]}},
        )
        self.assertEqual(direct.status_code, 409, direct.text)

        for arguments in (
            {"site_ids": []},
            {"site_ids": ["unknown"]},
            {"site_ids": ["nyaa"], "key": "INDEXER_ENABLED_SITES"},
        ):
            with self.subTest(arguments=arguments):
                invalid = self.client.post(
                    "/api/agent/actions/config.set_indexer_sites/prepare",
                    headers=headers,
                    json={"session_id": "test_session_identifier_0001", "arguments": arguments},
                )
                self.assertEqual(invalid.status_code, 400, invalid.text)

        noop = self.client.post(
            "/api/agent/actions/config.set_indexer_sites/prepare",
            headers=headers,
            json={"session_id": "test_session_identifier_0001", "arguments": {"site_ids": ["mikan", "nyaa"]}},
        )
        self.assertEqual(noop.status_code, 409, noop.text)

        for key in ("INDEXER_ENABLED_SITES", "INDEXER_SUKEBEI_ENABLED"):
            with self.subTest(key=key):
                agent_rate_limiter.reset()
                config._STARTUP_ENV_OVERRIDES = frozenset({key})
                os.environ[key] = "nyaa"
                overridden = self.client.post(
                    "/api/agent/actions/config.set_indexer_sites/prepare",
                    headers=headers,
                    json={"session_id": "test_session_identifier_0001", "arguments": {"site_ids": ["nyaa", "tpb"]}},
                )
                self.assertEqual(overridden.status_code, 409, overridden.text)
                self.assertIn("运行环境", overridden.text)
                os.environ.pop(key, None)
                config._STARTUP_ENV_OVERRIDES = frozenset()

    def test_confirm_rejects_stale_snapshot_without_writing(self):
        csrf = self.login()
        prepared = self.prepare(csrf, ["nyaa", "tpb"])
        confirmation_id = prepared.json()["action_plan"]["plan_id"]
        values = config._read_env_file(self.env_file)
        values["UNRELATED_SETTING"] = "changed"
        config.write_env_file(self.env_file, values, replace=True)
        stale = self.client.post(
            "/api/agent/actions/confirm",
            headers={"X-CSRF-Token": csrf},
            json={"session_id": "test_session_identifier_0001", "plan_id": confirmation_id},
        )
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertIn("配置已变化", stale.text)
        self.assertEqual(
            config._read_env_file(self.env_file)["INDEXER_ENABLED_SITES"],
            "nyaa,mikan",
        )

    def test_atomic_write_failures_do_not_refresh_or_leak(self):
        csrf = self.login()
        original = self.env_file.read_bytes()
        cases = (
            (config.ConcurrentConfigUpdateError("simulated"), 409, "conflict"),
            (config.AtomicPublishError("simulated"), 503, "unavailable"),
            (OSError("simulated"), 503, "unavailable"),
        )
        for error, expected_status, expected_result_status in cases:
            with self.subTest(error=type(error).__name__):
                prepared = self.prepare(csrf, ["nyaa", "tpb"])
                confirmation_id = prepared.json()["action_plan"]["plan_id"]
                with patch(
                    "app.agent.indexer_config_actions.config.update_runtime_env_file",
                    side_effect=error,
                ), patch("app.indexers.runtime.shutdown_indexer_service") as shutdown, patch(
                    "app.modules.telegram_resource_search.shutdown_telegram_indexer_worker"
                ) as shutdown_telegram:
                    confirmed = self.client.post(
                        "/api/agent/actions/confirm",
                        headers={"X-CSRF-Token": csrf},
                        json={"session_id": "test_session_identifier_0001", "plan_id": confirmation_id},
                    )
                self.assertEqual(confirmed.status_code, expected_status, confirmed.text)
                self.assertEqual(
                    confirmed.json()["result"]["status"], expected_result_status
                )
                self.assertEqual(self.env_file.read_bytes(), original)
                self.assertNotIn("must-not-leak", confirmed.text)
                shutdown.assert_not_called()
                shutdown_telegram.assert_not_called()

    def test_runtime_refresh_failure_reports_deferred_success(self):
        csrf = self.login()
        prepared = self.prepare(csrf, ["nyaa", "tpb"])
        confirmation_id = prepared.json()["action_plan"]["plan_id"]
        with patch(
            "app.indexers.runtime.shutdown_indexer_service",
            side_effect=RuntimeError("simulated"),
        ), patch(
            "app.modules.telegram_resource_search.shutdown_telegram_indexer_worker"
        ) as shutdown_telegram:
            confirmed = self.client.post(
                "/api/agent/actions/confirm",
                headers={"X-CSRF-Token": csrf},
                json={"session_id": "test_session_identifier_0001", "plan_id": confirmation_id},
            )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        result = confirmed.json()["result"]
        self.assertTrue(result["ok"])
        self.assertFalse(result["data"]["runtime_refreshed"])
        shutdown_telegram.assert_called_once_with(timeout=5.0)
        self.assertEqual(
            result["data"]["runtime_refresh"],
            {"web": False, "telegram": True},
        )
        self.assertIn("重启当前服务", " ".join(result["suggestions"]))
        self.assertEqual(
            config._read_env_file(self.env_file)["INDEXER_ENABLED_SITES"],
            "nyaa,tpb",
        )

    def test_telegram_refresh_timeout_reports_deferred_success(self):
        csrf = self.login()
        prepared = self.prepare(csrf, ["nyaa", "tpb"])
        confirmation_id = prepared.json()["action_plan"]["plan_id"]
        with patch("app.indexers.runtime.shutdown_indexer_service") as shutdown, patch(
            "app.modules.telegram_resource_search.shutdown_telegram_indexer_worker",
            return_value=False,
        ) as shutdown_telegram:
            confirmed = self.client.post(
                "/api/agent/actions/confirm",
                headers={"X-CSRF-Token": csrf},
                json={"session_id": "test_session_identifier_0001", "plan_id": confirmation_id},
            )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        result = confirmed.json()["result"]
        self.assertTrue(result["ok"])
        self.assertFalse(result["data"]["runtime_refreshed"])
        shutdown.assert_awaited_once()
        shutdown_telegram.assert_called_once_with(timeout=5.0)
        self.assertEqual(
            result["data"]["runtime_refresh"],
            {"web": True, "telegram": False},
        )
        self.assertIn("Telegram Bot", " ".join(result["suggestions"]))
        self.assertNotIn("下次请求", " ".join(result["suggestions"]))

    def test_web_refresh_timeout_is_bounded_and_reports_restart(self):
        csrf = self.login()
        prepared = self.prepare(csrf, ["nyaa", "tpb"])
        confirmation_id = prepared.json()["action_plan"]["plan_id"]

        async def delayed_shutdown():
            await asyncio.sleep(1)

        with patch(
            "app.indexers.runtime.shutdown_indexer_service",
            side_effect=delayed_shutdown,
        ), patch(
            "app.agent.indexer_config_actions._RUNTIME_REFRESH_TIMEOUT_SECONDS",
            0.01,
        ), patch(
            "app.modules.telegram_resource_search.shutdown_telegram_indexer_worker",
            return_value=True,
        ) as shutdown_telegram:
            confirmed = self.client.post(
                "/api/agent/actions/confirm",
                headers={"X-CSRF-Token": csrf},
                json={"session_id": "test_session_identifier_0001", "plan_id": confirmation_id},
            )

        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        result = confirmed.json()["result"]
        self.assertTrue(result["ok"])
        self.assertFalse(result["data"]["runtime_refreshed"])
        self.assertEqual(
            result["data"]["runtime_refresh"],
            {"web": False, "telegram": True},
        )
        shutdown_telegram.assert_called_once_with(timeout=0.01)
        self.assertIn("重启当前服务", " ".join(result["suggestions"]))

    def test_direct_prepare_and_query_share_strict_write_rate_limit(self):
        csrf = self.login()
        headers = {"X-CSRF-Token": csrf}
        for _ in range(2):
            direct = self.client.post(
                "/api/agent/actions/config.set_indexer_sites/prepare",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "arguments": {"site_ids": ["nyaa", "tpb"]}},
            )
            self.assertEqual(direct.status_code, 200, direct.text)
            query = self.client.post(
                "/api/agent/query",
                headers=headers,
                json={"session_id": "test_session_identifier_0001", "message": "设置资源站点为 Nyaa、TPB"},
            )
            self.assertEqual(query.status_code, 200, query.text)
        limited = self.client.post(
            "/api/agent/query",
            headers=headers,
            json={"session_id": "test_session_identifier_0001", "message": "设置资源站点为 Nyaa、TPB"},
        )
        self.assertEqual(limited.status_code, 429, limited.text)

    def test_confirm_requires_csrf(self):
        self.login()
        response = self.client.post(
            "/api/agent/actions/confirm",
            json={"session_id": "test_session_identifier_0001", "plan_id": "x" * 24},
        )
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
