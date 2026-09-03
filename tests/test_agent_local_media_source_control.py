"""本地媒体来源触发器的安全 Agent 控制合同。"""

from __future__ import annotations

import json
from unittest.mock import Mock, patch

from app import database as db
from app.agent.action_history import action_history_owner_digest
from app.agent.errors import AgentToolError
from app.agent.local_media_source_actions import (
    local_media_source_summaries_arguments,
    local_media_source_summary_arguments,
    local_media_source_trigger_arguments,
)
from app.agent.models import RiskLevel
from app.agent.rate_limit import agent_rate_limiter
from tests.agent_kernel_test_harness import (
    build_kernel_test_registry as build_tool_registry,
)
from tests.agent_kernel_test_harness import (
    get_kernel_test_service as get_agent_service,
)
from tests.agent_kernel_test_harness import (
    reset_kernel_test_service as reset_agent_service_for_tests,
)
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
                {"source_number": 1, "trigger": "QB_COMPLETED", "enabled": True}
            ),
            {"source_number": 1, "trigger": "qb_completed", "enabled": True},
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
                {"source_number": 1, "trigger": "scan", "enabled": True},
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
            capabilities["local_media.source_summaries"]["risk"], RiskLevel.READ.value
        )
        self.assertFalse(
            capabilities["local_media.get_source_summary"]["requires_confirmation"]
        )
        self.assertEqual(
            capabilities["local_media.set_source_trigger_enabled"]["risk"],
            RiskLevel.LOW_WRITE.value,
        )
        self.assertTrue(
            capabilities["local_media.set_source_trigger_enabled"][
                "requires_confirmation"
            ]
        )

    def test_prepare_confirm_updates_only_requested_trigger_and_records_safe_history(
        self,
    ) -> None:
        service = get_agent_service()
        before = db.get_local_media_source(self.source_one)
        prepared = service.prepare(
            "local_media.set_source_trigger_enabled",
            {"source_number": 1, "trigger": "qb_completed", "enabled": False},
            owner="owner",
        )
        after_prepare = db.get_local_media_source(self.source_one)
        self.assertTrue(before.enabled)
        self.assertTrue(after_prepare.enabled)
        self.assertEqual(prepared["mode"], "confirmation_required")
        self.assertEqual(
            prepared["action_plan"]["confirmation"]["action"],
            "切换本地媒体来源触发方式",
        )
        scheduler = Mock()
        with patch(
            "app.agent.local_media_source_actions.get_local_media_scheduler",
            return_value=scheduler,
        ):
            confirmed = service.confirm(
                prepared["action_plan"]["plan_id"], owner="owner"
            )
        self.assertTrue(confirmed["result"]["ok"])
        self.assertFalse(confirmed["result"]["data"]["enabled"])
        scheduler.reload.assert_called_once_with()
        changed = db.get_local_media_source(self.source_one)
        self.assertFalse(changed.enabled)
        self.assertFalse(changed.scan_enabled)
        self.assertEqual(changed.local_root, before.local_root)
        self.assertEqual(changed.qb_profile, before.qb_profile)
        history = db.list_agent_action_history(
            owner_digest=action_history_owner_digest("owner"), limit=1
        )[0]
        self.assertEqual(history["tool_name"], "local_media.set_source_trigger_enabled")
        details = json.loads(history["safe_details"])
        self.assertEqual(
            {
                key: details[key]
                for key in (
                    "operation",
                    "source_number",
                    "trigger",
                    "enabled",
                    "affected",
                    "runtime_refreshed",
                )
            },
            {
                "operation": "disable",
                "source_number": 1,
                "trigger": "qb_completed",
                "enabled": False,
                "affected": 1,
                "runtime_refreshed": True,
            },
        )
        serialized = json.dumps(dict(history), ensure_ascii=False)
        for secret in ("PRIVATE-", "/private", "jellyfin"):
            self.assertNotIn(secret, serialized)

    def test_stale_snapshot_ordinal_drift_and_one_time_ticket_are_rejected(
        self,
    ) -> None:
        service = get_agent_service()
        prepared = service.prepare(
            "local_media.set_source_trigger_enabled",
            {"source_number": 1, "trigger": "qb_completed", "enabled": False},
            owner="owner",
        )
        db.update_local_media_source(self.source_one, media_type="movie")
        stale = service.confirm(prepared["action_plan"]["plan_id"], owner="owner")
        self.assertFalse(stale["result"]["ok"])
        self.assertEqual(stale["result"]["status"], "conflict")
        with self.assertRaises(AgentToolError):
            service.confirm(prepared["action_plan"]["plan_id"], owner="owner")
        reset_agent_service_for_tests()
        service = get_agent_service()
        ordinal_ticket = service.prepare(
            "local_media.set_source_trigger_enabled",
            {"source_number": 2, "trigger": "qb_completed", "enabled": True},
            owner="owner",
        )
        self.assertTrue(db.delete_local_media_source(self.source_one))
        ordinal = service.confirm(
            ordinal_ticket["action_plan"]["plan_id"], owner="owner"
        )
        self.assertFalse(ordinal["result"]["ok"])
        self.assertEqual(ordinal["result"]["status"], "conflict")
        remaining = db.get_local_media_source(self.source_two)
        self.assertFalse(remaining.enabled)
