"""本地媒体来源触发器的安全 Agent 控制合同。"""
from __future__ import annotations

import json
from unittest.mock import Mock, patch

from app import database as db
from app.agent.action_history import action_history_owner_digest
from app.agent.llm_router import read_plan_capabilities, read_tool_capabilities
from app.agent.local_media_source_actions import (
    get_local_media_source_summary,
    list_local_media_source_summaries,
    local_media_source_summaries_arguments,
    local_media_source_summary_arguments,
    local_media_source_trigger_arguments,
)
from app.agent.models import RiskLevel
from app.agent.local_media_intents import (
    is_local_media_source_summaries_message,
    is_local_media_source_trigger_control_message,
    local_media_source_summary_request,
    local_media_source_trigger_control_request,
)
from app.agent.orchestrator import AgentOrchestrator
from app.agent.rate_limit import agent_rate_limiter, tool_rate_limit_policy
from app.agent.registry import AgentToolError
from app.agent.result_projection import project_agent_response_for_llm
from app.agent.service import get_agent_service, reset_agent_service_for_tests
from app.agent.tools import build_tool_registry
from tests.support import IsolatedDatabaseTestCase


class LocalMediaSourceAgentControlTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM local_media_tasks")
            conn.execute("DELETE FROM local_library_targets")
            conn.execute("DELETE FROM local_media_sources")
            conn.execute("DELETE FROM agent_action_history")
        reset_agent_service_for_tests()
        agent_rate_limiter.reset()
        self.source_one = db.create_local_media_source(
            name="PRIVATE-SOURCE-ONE",
            qb_profile="PRIVATE-QB-PROFILE",
            qb_path_prefix="/private/qb/one",
            local_root="/private/local/one",
            enabled=1,
            stable_seconds=300,
            scan_enabled=0,
            scan_interval_minutes=15,
            media_type="tv",
            mode="move",
        )
        db.upsert_local_library_target(
            self.source_one,
            "tv",
            "/private/library/tv",
            provider="jellyfin",
            library_name="PRIVATE-LIBRARY-NAME",
            library_id="PRIVATE-LIBRARY-ID",
        )
        self.source_two = db.create_local_media_source(
            name="PRIVATE-SOURCE-TWO",
            qb_profile="PRIVATE-QB-PROFILE-TWO",
            qb_path_prefix="/private/qb/two",
            local_root="/private/local/two",
            enabled=0,
            stable_seconds=600,
            scan_enabled=1,
            scan_interval_minutes=30,
            media_type="movie",
            mode="preview_only",
        )

    def tearDown(self) -> None:
        agent_rate_limiter.reset()
        reset_agent_service_for_tests()

    def test_arguments_and_registry_contract_are_strict(self) -> None:
        self.assertEqual(local_media_source_summaries_arguments({}), {})
        self.assertEqual(
            local_media_source_summary_arguments({"source_number": 2}),
            {"source_number": 2},
        )
        self.assertEqual(
            local_media_source_trigger_arguments(
                {"source_number": 1, "trigger": "SCAN", "enabled": True}
            ),
            {"source_number": 1, "trigger": "scan", "enabled": True},
        )
        invalid_cases = (
            (local_media_source_summaries_arguments, {"path": "/private"}),
            (local_media_source_summary_arguments, {"source_number": True}),
            (local_media_source_summary_arguments, {"source_number": 1, "extra": 1}),
            (
                local_media_source_trigger_arguments,
                {"source_number": 1, "trigger": "all", "enabled": True},
            ),
            (
                local_media_source_trigger_arguments,
                {"source_number": 1, "trigger": "scan", "enabled": 1},
            ),
        )
        for validator, arguments in invalid_cases:
            with self.subTest(validator=validator.__name__, arguments=arguments):
                with self.assertRaises(AgentToolError):
                    validator(arguments)

        registry = build_tool_registry()
        capabilities = {item["name"]: item for item in registry.capabilities()}
        self.assertEqual(
            capabilities["local_media.source_summaries"]["risk"],
            RiskLevel.READ.value,
        )
        self.assertFalse(
            capabilities["local_media.get_source_summary"]["requires_confirmation"]
        )
        self.assertEqual(
            capabilities["local_media.set_source_trigger_enabled"]["risk"],
            RiskLevel.LOW_WRITE.value,
        )
        self.assertTrue(
            capabilities["local_media.set_source_trigger_enabled"]["requires_confirmation"]
        )

    def test_deterministic_parser_routes_exact_and_ambiguous_requests(self) -> None:
        self.assertEqual(
            local_media_source_summary_request("查看本地媒体来源 2 详情"),
            {"source_number": 2},
        )
        self.assertTrue(is_local_media_source_summaries_message("列出本地媒体来源"))
        self.assertEqual(
            local_media_source_trigger_control_request(
                "暂停本地媒体来源 2 的 qB 下载完成自动接管".casefold()
            ),
            (
                "local_media.set_source_trigger_enabled",
                {"source_number": 2, "trigger": "qb_completed", "enabled": False},
            ),
        )
        self.assertEqual(
            local_media_source_trigger_control_request(
                "启用本地媒体来源 2 的目录自动扫描"
            ),
            (
                "local_media.set_source_trigger_enabled",
                {"source_number": 2, "trigger": "scan", "enabled": True},
            ),
        )
        self.assertIsNone(
            local_media_source_trigger_control_request("暂停所有本地媒体来源的目录扫描")
        )
        self.assertTrue(
            is_local_media_source_trigger_control_message(
                "暂停所有本地媒体来源的目录扫描"
            )
        )
        self.assertIsNone(
            local_media_source_trigger_control_request("暂停本地媒体来源 2")
        )
        self.assertTrue(
            is_local_media_source_trigger_control_message("暂停本地媒体来源 2")
        )

    def test_list_detail_and_llm_projection_do_not_leak_private_configuration(self) -> None:
        listed = list_local_media_source_summaries({})
        detail = get_local_media_source_summary({"source_number": 1})
        self.assertTrue(listed.ok)
        self.assertEqual(listed.data["total"], 2)
        self.assertEqual(listed.data["enabled_count"], 1)
        self.assertEqual(listed.data["scan_enabled_count"], 0)
        self.assertEqual(detail.data["source_number"], 1)
        self.assertEqual(detail.data["target_categories"], ["tv"])

        response = {
            "tool_call": {"name": "local_media.source_summaries", "arguments": {}},
            "result": listed.to_dict(),
        }
        projected = project_agent_response_for_llm(response)
        serialized = json.dumps(
            {"listed": listed.to_dict(), "detail": detail.to_dict(), "projected": projected},
            ensure_ascii=False,
        )
        for secret in (
            "PRIVATE-SOURCE",
            "PRIVATE-QB",
            "/private",
            "PRIVATE-LIBRARY",
        ):
            self.assertNotIn(secret, serialized)
        self.assertIn("source_number", serialized)
        self.assertIn("scan_enabled", serialized)

    def test_prepare_confirm_updates_only_requested_trigger_and_records_safe_history(self) -> None:
        service = get_agent_service()
        before = db.get_local_media_source(self.source_one)
        prepared = service.prepare(
            "local_media.set_source_trigger_enabled",
            {"source_number": 1, "trigger": "scan", "enabled": True},
            owner="owner",
        )
        after_prepare = db.get_local_media_source(self.source_one)
        self.assertFalse(before.scan_enabled)
        self.assertFalse(after_prepare.scan_enabled)
        self.assertEqual(prepared["mode"], "confirmation_required")
        self.assertEqual(
            prepared["confirmation"]["contract"]["action"],
            "切换本地媒体来源触发方式",
        )

        scheduler = Mock()
        with patch(
            "app.agent.local_media_source_actions.get_local_media_scheduler",
            return_value=scheduler,
        ):
            confirmed = service.confirm(
                prepared["confirmation"]["confirmation_id"], owner="owner"
            )
        self.assertTrue(confirmed["result"]["ok"])
        self.assertTrue(confirmed["result"]["data"]["enabled"])
        scheduler.reload.assert_called_once_with()
        changed = db.get_local_media_source(self.source_one)
        self.assertTrue(changed.enabled)
        self.assertTrue(changed.scan_enabled)
        self.assertEqual(changed.local_root, before.local_root)
        self.assertEqual(changed.qb_profile, before.qb_profile)

        history = db.list_agent_action_history(
            owner_digest=action_history_owner_digest("owner"), limit=1
        )[0]
        self.assertEqual(history["tool_name"], "local_media.set_source_trigger_enabled")
        details = json.loads(history["safe_details"])
        self.assertEqual(
            {key: details[key] for key in (
                "operation", "source_number", "trigger", "enabled", "affected",
                "runtime_refreshed",
            )},
            {
                "operation": "enable",
                "source_number": 1,
                "trigger": "scan",
                "enabled": True,
                "affected": 1,
                "runtime_refreshed": True,
            },
        )
        serialized = json.dumps(dict(history), ensure_ascii=False)
        for secret in ("PRIVATE-", "/private", "jellyfin"):
            self.assertNotIn(secret, serialized)

    def test_stale_snapshot_ordinal_drift_and_one_time_ticket_are_rejected(self) -> None:
        service = get_agent_service()
        prepared = service.prepare(
            "local_media.set_source_trigger_enabled",
            {"source_number": 1, "trigger": "scan", "enabled": True},
            owner="owner",
        )
        db.update_local_media_source(self.source_one, stable_seconds=301)
        stale = service.confirm(
            prepared["confirmation"]["confirmation_id"], owner="owner"
        )
        self.assertFalse(stale["result"]["ok"])
        self.assertEqual(stale["result"]["status"], "conflict")
        with self.assertRaises(AgentToolError):
            service.confirm(prepared["confirmation"]["confirmation_id"], owner="owner")

        reset_agent_service_for_tests()
        service = get_agent_service()
        ordinal_ticket = service.prepare(
            "local_media.set_source_trigger_enabled",
            {"source_number": 2, "trigger": "qb_completed", "enabled": True},
            owner="owner",
        )
        self.assertTrue(db.delete_local_media_source(self.source_one))
        ordinal = service.confirm(
            ordinal_ticket["confirmation"]["confirmation_id"], owner="owner"
        )
        self.assertFalse(ordinal["result"]["ok"])
        self.assertEqual(ordinal["result"]["status"], "conflict")
        remaining = db.get_local_media_source(self.source_two)
        self.assertFalse(remaining.enabled)

    def test_read_routes_preserve_existing_owner_and_rate_identity_contracts(self) -> None:
        service = AgentOrchestrator(Mock())
        cases = (
            (
                "查看本地媒体来源 2 详情",
                ("local_media.get_source_summary", {"source_number": 2}),
                {},
            ),
            (
                "查看本地媒体来源",
                ("local_media.source_summaries", {}),
                {},
            ),
            (
                "查看本地媒体待确认统计",
                ("local_media.review_queue_summary",),
                {"owner": "owner", "rate_identity": "rate-owner"},
            ),
            (
                "查看本地整理处理历史",
                ("local_media.history_summary",),
                {"owner": "owner", "rate_identity": "rate-owner"},
            ),
        )

        for message, expected_args, expected_kwargs in cases:
            with self.subTest(message=message), patch.object(
                service,
                "_invoke_query_read",
                return_value={"handled": message},
            ) as invoke_read:
                response = service._query_raw(
                    message,
                    owner="owner",
                    query_tool_rate_identity="rate-owner",
                )

            self.assertEqual(response, {"handled": message})
            invoke_read.assert_called_once_with(*expected_args, **expected_kwargs)

        with patch.object(
            service,
            "_invoke_query_read",
            return_value={"handled": "diagnosis"},
        ) as invoke_read:
            diagnosis = service._query_raw(
                "检查本地媒体配置状态",
                owner="owner",
                query_tool_rate_identity="rate-owner",
            )

        self.assertEqual(diagnosis, {"handled": "diagnosis"})
        invoke_read.assert_called_once_with("local_media.diagnose", {})

    def test_precondition_orchestrator_capabilities_and_rate_limit_policy(self) -> None:
        service = get_agent_service()
        with self.assertRaises(AgentToolError) as same_state:
            service.prepare(
                "local_media.set_source_trigger_enabled",
                {"source_number": 1, "trigger": "qb_completed", "enabled": True},
                owner="owner",
            )
        self.assertEqual(same_state.exception.code, "precondition_failed")

        listed = service.query("查看本地媒体来源", owner="owner")
        self.assertEqual(listed["tool_call"]["name"], "local_media.source_summaries")
        detail = service.query("查看本地媒体来源 2 详情", owner="owner")
        self.assertEqual(detail["tool_call"]["name"], "local_media.get_source_summary")
        prepared = service.query(
            "启用本地媒体来源 2 的 qB 下载完成自动接管", owner="owner"
        )
        self.assertEqual(prepared["mode"], "confirmation_required")
        self.assertEqual(
            prepared["confirmation"]["tool"],
            "local_media.set_source_trigger_enabled",
        )
        ambiguous = service.query("暂停所有本地媒体来源的目录自动扫描", owner="owner")
        self.assertEqual(ambiguous["mode"], "clarification")
        self.assertEqual(ambiguous["result"]["status"], "clarification_required")
        unauthenticated = service.query("启用本地媒体来源 2 的目录自动扫描")
        self.assertEqual(unauthenticated["mode"], "read_only")
        self.assertEqual(unauthenticated["result"]["status"], "unsupported")

        registry = build_tool_registry()
        read_names = {item["name"] for item in read_tool_capabilities(registry)}
        plan_names = {item["name"] for item in read_plan_capabilities(registry)}
        self.assertIn("local_media.source_summaries", read_names)
        self.assertIn("local_media.get_source_summary", read_names)
        self.assertIn("local_media.source_summaries", plan_names)
        self.assertIn("local_media.get_source_summary", plan_names)
        self.assertNotIn("local_media.set_source_trigger_enabled", read_names)
        self.assertNotIn("local_media.set_source_trigger_enabled", plan_names)
        self.assertEqual(
            tool_rate_limit_policy("local_media.source_summaries"),
            ("local-media-source-read", 8, 1),
        )
        self.assertEqual(
            tool_rate_limit_policy("local_media.get_source_summary"),
            ("local-media-source-read", 8, 1),
        )
        self.assertEqual(
            tool_rate_limit_policy("local_media.set_source_trigger_enabled"),
            ("local-media-source-control", 4, 1),
        )
