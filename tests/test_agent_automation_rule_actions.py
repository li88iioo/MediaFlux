"""规则只在确认后持久化，沿既有领域生命周期执行。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock, patch

from app import config
from app import database as db
from app.agent.automation_rule_actions import (
    create_media_rule_arguments,
    create_media_rule_confirmed,
    digest_set_arguments,
    list_digest_rules,
    prepare_create_media_rule,
    prepare_set_digest,
    set_digest_confirmed,
)
from app.agent.errors import AgentToolError
from app.agent.models import ToolContext, ToolResult
from app.agent.owner_routes import web_kernel_owner
from app.modules.media_automation_rules import drain_automation_rules, next_summary_at
from app.repositories import media_automation_rules as rules
from tests.support import IsolatedDatabaseTestCase


class AutomationRuleTests(IsolatedDatabaseTestCase):
    def setUp(self):
        enabled = patch(
            "app.modules.media_automation_rules.is_agent_enabled", return_value=True
        )
        enabled.start()
        self.addCleanup(enabled.stop)
        rules.ensure_schema()
        with db.get_conn() as conn:
            conn.execute("DELETE FROM media_automation_rules")
        self.context = ToolContext(
            owner=web_kernel_owner("owner-one"), session_id="test-session"
        )
        original_get = config.get
        route_config = patch(
            "app.modules.media_automation_rules.config.get",
            side_effect=lambda key, *args, **kwargs: (
                "123" if key == "TG_CHAT_ID" else original_get(key, *args, **kwargs)
            ),
        )
        route_config.start()
        self.addCleanup(route_config.stop)
        self.clock = datetime(2026, 9, 5, 21, 0, tzinfo=timezone(timedelta(hours=8)))

    def test_digest_prepares_without_writing_and_ownership_rejects(self):
        args = {"enabled": True, "hour": 21}
        preview, token = prepare_set_digest(args, self.context)
        self.assertEqual(preview.status, "confirmation_required")
        self.assertEqual(list_digest_rules({}, self.context).data["items"], [])
        with self.assertRaises(AgentToolError):
            set_digest_confirmed(args, token, ToolContext(owner="other"))
        saved = set_digest_confirmed(args, token, self.context)
        self.assertTrue(saved.ok)
        self.assertEqual(len(list_digest_rules({}, self.context).data["items"]), 1)
        self.assertEqual(
            list_digest_rules({}, ToolContext(owner="other")).data["items"], []
        )

    def test_digest_update_cas_and_tampered_arguments(self):
        args = {"enabled": True, "hour": 21}
        _, token = prepare_set_digest(args, self.context)
        first = set_digest_confirmed(args, token, self.context).data
        change = {"rule_id": first["rule_id"], "enabled": False, "hour": 21}
        _, old_token = prepare_set_digest(change, self.context)
        with self.assertRaises(AgentToolError):
            set_digest_confirmed({**change, "hour": 20}, old_token, self.context)
        self.assertTrue(set_digest_confirmed(change, old_token, self.context).ok)
        with self.assertRaises(AgentToolError):
            set_digest_confirmed(change, old_token, self.context)

    def test_rule_claim_is_exclusive_recovers_expired_and_cancel_invalidates(self):
        row = rules.save_rule(
            "owner",
            kind="daily_summary",
            settings={"hour": 21, "minute": 0},
            enabled=True,
            next_run_at=self.clock.isoformat(),
        )
        first = rules.claim_due_rules(self.clock)
        self.assertEqual(len(first), 1)
        self.assertEqual(rules.claim_due_rules(self.clock), [])
        second = rules.claim_due_rules(self.clock + timedelta(minutes=6))
        self.assertEqual(len(second), 1)
        self.assertFalse(
            rules.finish_rule(
                row["id"], first[0]["lease_token"], self.clock.isoformat()
            )
        )
        self.assertTrue(rules.delete_rule("owner", row["id"], expected_revision=1))
        self.assertFalse(rules.owns_lease(row["id"], second[0]["lease_token"]))

    @patch(
        "app.modules.media_automation_rules._authorized_notification_chat",
        return_value="123",
    )
    def test_daily_delivery_reuses_notification_center_and_runs_once(self, _route):
        row = rules.save_rule(
            "owner",
            kind="daily_summary",
            settings={"hour": 21, "minute": 0},
            enabled=True,
            next_run_at=self.clock.isoformat(),
        )
        summary = {
            "local_date": "2026-09-05",
            "downloads": {"success": 2, "failed": 1},
            "content_titles": ["测试作品"],
        }
        with (
            patch(
                "app.modules.media_automation_rules.today_content_summary",
                return_value=summary,
            ),
            patch(
                "app.modules.media_automation_rules.publish_notification_event",
                return_value=Mock(status="queued"),
            ) as publish,
        ):
            self.assertEqual(drain_automation_rules(now=self.clock), 1)
            self.assertEqual(drain_automation_rules(now=self.clock), 0)
        self.assertEqual(publish.call_count, 1)
        self.assertFalse(publish.call_args.kwargs["deliver_now"])
        self.assertIn(row["id"], publish.call_args.args[0])
        self.assertNotIn("LLM", publish.call_args.args[1].title)

    @patch(
        "app.modules.media_automation_rules._authorized_notification_chat",
        return_value="123",
    )
    def test_errors_only_quiet_day_skips_message(self, _route):
        rules.save_rule(
            "owner",
            kind="daily_summary",
            settings={"hour": 21, "minute": 0, "errors_only": True},
            enabled=True,
            next_run_at=self.clock.isoformat(),
        )
        with (
            patch(
                "app.modules.media_automation_rules.today_content_summary",
                return_value={"downloads": {"success": 2}},
            ),
            patch(
                "app.modules.media_automation_rules.publish_notification_event"
            ) as publish,
        ):
            self.assertEqual(drain_automation_rules(now=self.clock), 1)
            publish.assert_not_called()

    @patch(
        "app.modules.media_automation_rules._authorized_notification_chat",
        return_value="123",
    )
    def test_outbox_failure_can_retry_with_same_deduplication_key(self, _route):
        rules.save_rule(
            "owner",
            kind="daily_summary",
            settings={"hour": 21, "minute": 0, "send_empty": True},
            enabled=True,
            next_run_at=self.clock.isoformat(),
        )
        declined = Mock(status="failed")
        declined.__bool__ = Mock(return_value=False)
        with (
            patch(
                "app.modules.media_automation_rules.today_content_summary",
                return_value={},
            ),
            patch(
                "app.modules.media_automation_rules.publish_notification_event",
                side_effect=[RuntimeError("offline"), Mock(status="queued")],
            ) as publish,
        ):
            self.assertEqual(drain_automation_rules(now=self.clock), 0)
            self.assertEqual(
                drain_automation_rules(now=self.clock + timedelta(minutes=6)), 1
            )
            self.assertEqual(
                publish.call_args_list[0].args[0], publish.call_args_list[1].args[0]
            )

    def test_summary_clock_rolls_to_next_day(self):
        self.assertEqual(
            next_summary_at({"hour": 21, "minute": 0}, self.clock),
            "2026-09-06T21:00:00+08:00",
        )

    def test_strict_schema_rejects_unsupported_scheduling_filters_and_credentials(self):
        for extra in (
            {"cron": "0 21 * * 6"},
            {"resolution": "2160p"},
            {"api_key": "private"},
        ):
            with self.assertRaises(AgentToolError):
                create_media_rule_arguments(
                    {"tmdb_id": "123", "media_type": "tv", **extra}
                )
        for args in (
            {"enabled": "yes", "hour": 21},
            {"enabled": True, "hour": True},
            {"enabled": True, "hour": 25},
        ):
            with self.assertRaises(AgentToolError):
                digest_set_arguments(args)

    def test_subscription_rule_uses_existing_service_once_after_confirmation(self):
        args = {
            "tmdb_id": "123",
            "media_type": "tv",
            "download_target": "qb",
            "action": "auto",
            "sites": ["nyaa"],
        }
        frozen = {
            "snapshot": {"exists": False, "tmdb_id": "123", "media_type": "tv"},
            "tmdb_id": "123",
        }
        import json

        with (
            patch(
                "app.agent.automation_rule_actions.prepare_create_media_subscription",
                return_value=(
                    ToolResult(True, "confirmation_required", "preview"),
                    json.dumps(frozen),
                ),
            ),
            patch(
                "app.agent.automation_rule_actions._create_snapshot",
                return_value=frozen["snapshot"],
            ),
            patch(
                "app.agent.automation_rule_actions.get_media_subscription_service"
            ) as get_service,
            patch(
                "app.agent.automation_rule_actions._reload_scheduler", return_value=True
            ),
        ):
            preview, token = prepare_create_media_rule(args)
            get_service.assert_not_called()
            expected = {
                "id": 9,
                "title": "作品",
                "tmdb_id": "123",
                "media_type": "tv",
                "action": "auto",
                "download_target": "qb",
                "sites": ["nyaa"],
                "enabled": True,
                "check_interval_minutes": 10080,
            }
            service = get_service.return_value
            service.create_subscription = AsyncMock(
                return_value={"subscription": expected}
            )
            service.get_subscription.return_value = expected
            result = create_media_rule_confirmed(args, token)
            self.assertTrue(result.ok)
            self.assertEqual(result.data["subscription_number"], 9)
            self.assertEqual(service.create_subscription.await_count, 1)
            self.assertIn("持续自动提交", preview.data["effects"][1])

    def test_changed_subscription_never_calls_service(self):
        args = {"tmdb_id": "123", "media_type": "tv"}
        import json

        with patch(
            "app.agent.automation_rule_actions.prepare_create_media_subscription",
            return_value=(
                ToolResult(True, "confirmation_required", "preview"),
                json.dumps({"snapshot": {"exists": False}}),
            ),
        ):
            _, token = prepare_create_media_rule(args)
        with (
            patch(
                "app.agent.automation_rule_actions._create_snapshot",
                return_value={"exists": True},
            ),
            patch(
                "app.agent.automation_rule_actions.get_media_subscription_service"
            ) as service,
        ):
            with self.assertRaises(AgentToolError):
                create_media_rule_confirmed(args, token)
            service.assert_not_called()

    @patch(
        "app.modules.media_automation_rules._authorized_notification_chat",
        return_value="123",
    )
    def test_cancel_during_read_discards_late_summary(self, _route):
        import threading

        row = rules.save_rule(
            "owner",
            kind="daily_summary",
            settings={"hour": 21, "minute": 0, "send_empty": True},
            enabled=True,
            next_run_at=self.clock.isoformat(),
        )
        reading, release = threading.Event(), threading.Event()

        def summary():
            reading.set()
            release.wait(3)
            return {}

        with (
            patch(
                "app.modules.media_automation_rules.today_content_summary",
                side_effect=summary,
            ),
            patch(
                "app.modules.media_automation_rules.publish_notification_event"
            ) as publish,
        ):
            worker = threading.Thread(
                target=lambda: drain_automation_rules(now=self.clock)
            )
            worker.start()
            try:
                self.assertTrue(reading.wait(2))
                self.assertTrue(
                    rules.delete_rule("owner", row["id"], expected_revision=1)
                )
            finally:
                release.set()
                worker.join(3)
            self.assertFalse(worker.is_alive())
            publish.assert_not_called()

    @patch(
        "app.modules.media_automation_rules._authorized_notification_chat",
        return_value="123",
    )
    def test_outbox_acceptance_and_cancel_are_serialized(self, _route):
        import threading

        row = rules.save_rule(
            "owner",
            kind="daily_summary",
            settings={"hour": 21, "minute": 0, "send_empty": True},
            enabled=True,
            next_run_at=self.clock.isoformat(),
        )
        publishing, release, cancelling, cancelled = (
            threading.Event() for _ in range(4)
        )

        def publish(*args, **kwargs):
            publishing.set()
            release.wait(3)
            return Mock(status="queued")

        def cancel():
            cancelling.set()
            rules.delete_rule("owner", row["id"], expected_revision=1)
            cancelled.set()

        with (
            patch(
                "app.modules.media_automation_rules.today_content_summary",
                return_value={},
            ),
            patch(
                "app.modules.media_automation_rules.publish_notification_event",
                side_effect=publish,
            ),
        ):
            worker = threading.Thread(
                target=lambda: drain_automation_rules(now=self.clock)
            )
            canceller = threading.Thread(target=cancel)
            worker.start()
            try:
                self.assertTrue(publishing.wait(2))
                canceller.start()
                self.assertTrue(cancelling.wait(2))
                self.assertFalse(cancelled.wait(0.05))
            finally:
                release.set()
                worker.join(3)
                canceller.join(3)
            self.assertFalse(worker.is_alive())
            self.assertFalse(canceller.is_alive())
            self.assertTrue(cancelled.is_set())

    def test_no_telegram_authorization_never_falls_back_to_global_chat(self):
        from app.modules.media_automation_rules import notification_route_settings

        with (
            patch(
                "app.modules.media_automation_rules.telegram_owner_route_is_currently_authorized",
                return_value=False,
            ),
            self.assertRaises(AgentToolError),
        ):
            notification_route_settings("tg:v1:123\x1f456")
        with self.assertRaises(AgentToolError):
            notification_route_settings("not-an-authenticated-owner")

    def test_web_rule_remains_bound_to_original_configured_chat(self):
        from app.modules.media_automation_rules import (
            _authorized_notification_chat,
            notification_route_settings,
        )
        from app.repositories.agent_jobs import agent_job_owner_digest

        settings = notification_route_settings(self.context.owner)
        rule = {
            "owner_digest": agent_job_owner_digest(self.context.owner),
            "settings": settings,
        }
        self.assertEqual(_authorized_notification_chat(rule), "123")
        with patch("app.modules.media_automation_rules.config.get", return_value="999"):
            self.assertEqual(_authorized_notification_chat(rule), "")
