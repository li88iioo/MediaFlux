"""规则只在确认后持久化，沿既有领域生命周期执行。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from app import config
from app import database as db
from app.modules.media_automation_rules import digest_arguments, digests, prepare_set_digest, set_digest
from app.modules.media_subscriptions import MediaSubscriptionError, subscription_create_arguments
from app.agent.errors import AgentToolError
from app.agent.models import ToolContext
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
        prepared = prepare_set_digest(args, owner=self.context.owner, delivery_route=self.context.delivery_route)
        preview, token = prepared["preview"], prepared["snapshot"]
        self.assertEqual(preview["status"], "confirmation_required")
        self.assertEqual(digests(owner=self.context.owner)["data"]["items"], [])
        with self.assertRaises(AgentToolError):
            set_digest(args, expected_snapshot=token, owner="other")
        saved = set_digest(args, expected_snapshot=token, owner=self.context.owner, delivery_route=self.context.delivery_route)
        self.assertTrue(saved["ok"])
        self.assertEqual(len(digests(owner=self.context.owner)["data"]["items"]), 1)
        self.assertEqual(
            digests(owner="other")["data"]["items"], []
        )

    def test_digest_update_cas_and_tampered_arguments(self):
        args = {"enabled": True, "hour": 21}
        prepared = prepare_set_digest(args, owner=self.context.owner, delivery_route=self.context.delivery_route)
        _, token = prepared["preview"], prepared["snapshot"]
        first = set_digest(args, expected_snapshot=token, owner=self.context.owner, delivery_route=self.context.delivery_route)["data"]
        change = {"rule_id": first["rule_id"], "enabled": False, "hour": 21}
        prepared = prepare_set_digest(change, owner=self.context.owner, delivery_route=self.context.delivery_route)
        _, old_token = prepared["preview"], prepared["snapshot"]
        with self.assertRaises(AgentToolError):
            set_digest({**change, "hour": 20}, expected_snapshot=old_token, owner=self.context.owner, delivery_route=self.context.delivery_route)
        self.assertTrue(set_digest(change, expected_snapshot=old_token, owner=self.context.owner, delivery_route=self.context.delivery_route)["ok"])
        with self.assertRaises(AgentToolError):
            set_digest(change, expected_snapshot=old_token, owner=self.context.owner, delivery_route=self.context.delivery_route)

    def test_digest_partial_edit_preserves_unspecified_options(self):
        args = {"enabled": True, "hour": 21, "minute": 17, "errors_only": True, "send_empty": True}
        prepared = prepare_set_digest(args, owner=self.context.owner, delivery_route=self.context.delivery_route)
        _, token = prepared["preview"], prepared["snapshot"]
        original = set_digest(args, expected_snapshot=token, owner=self.context.owner, delivery_route=self.context.delivery_route)["data"]
        change = {"rule_id": original["rule_id"], "enabled": True, "hour": 22}
        prepared = prepare_set_digest(change, owner=self.context.owner, delivery_route=self.context.delivery_route)
        preview, token = prepared["preview"], prepared["snapshot"]
        for key, value in (("minute", 17), ("errors_only", True), ("send_empty", True)):
            self.assertEqual(preview["data"][key], value)
        updated = set_digest(change, expected_snapshot=token, owner=self.context.owner, delivery_route=self.context.delivery_route)
        self.assertEqual(updated["data"]["settings"], {
            "hour": 22, "minute": 17, "errors_only": True, "send_empty": True,
        })
        explicit = {**change, "minute": 0, "errors_only": False, "send_empty": False}
        prepared = prepare_set_digest(explicit, owner=self.context.owner, delivery_route=self.context.delivery_route)
        _, token = prepared["preview"], prepared["snapshot"]
        reset = set_digest(explicit, expected_snapshot=token, owner=self.context.owner, delivery_route=self.context.delivery_route)
        self.assertEqual(reset["data"]["settings"], {
            "hour": 22, "minute": 0, "errors_only": False, "send_empty": False,
        })
        self.assertEqual(len(digests(owner=self.context.owner)["data"]["items"]), 1)

    def test_existing_digest_can_stop_without_a_notification_destination(self):
        args = {"enabled": True, "hour": 21, "minute": 17, "errors_only": True}
        prepared = prepare_set_digest(args, owner=self.context.owner, delivery_route=self.context.delivery_route)
        _, token = prepared["preview"], prepared["snapshot"]
        original = set_digest(args, expected_snapshot=token, owner=self.context.owner, delivery_route=self.context.delivery_route)["data"]
        stop = {"rule_id": original["rule_id"], "enabled": False, "hour": 21}
        original_get = config.get
        with patch("app.modules.media_automation_rules.config.get", side_effect=lambda key, *a, **kw: (
            "" if key == "TG_CHAT_ID" else original_get(key, *a, **kw)
        )):
            prepared = prepare_set_digest(stop, owner=self.context.owner, delivery_route=self.context.delivery_route)
            preview, token = prepared["preview"], prepared["snapshot"]
            self.assertFalse(preview["data"]["enabled"])
            stopped = set_digest(stop, expected_snapshot=token, owner=self.context.owner, delivery_route=self.context.delivery_route)
            self.assertTrue(stopped["ok"])
            self.assertFalse(stopped["data"]["enabled"])
            self.assertEqual(stopped["data"]["settings"]["minute"], 17)
            self.assertTrue(stopped["data"]["settings"]["errors_only"])
            with self.assertRaises(AgentToolError):
                prepare_set_digest({**stop, "enabled": True}, owner=self.context.owner, delivery_route=self.context.delivery_route)
            with self.assertRaises(AgentToolError):
                prepare_set_digest({**stop, "rule_id": "missing-rule"}, owner=self.context.owner, delivery_route=self.context.delivery_route)
        self.assertEqual(len(digests(owner=self.context.owner)["data"]["items"]), 1)

    def test_digest_destination_change_still_invalidates_enable_confirmation(self):
        args = {"enabled": True, "hour": 21}
        prepared = prepare_set_digest(args, owner=self.context.owner, delivery_route=self.context.delivery_route)
        _, token = prepared["preview"], prepared["snapshot"]
        original_get = config.get
        with patch("app.modules.media_automation_rules.config.get", side_effect=lambda key, *a, **kw: (
            "999" if key == "TG_CHAT_ID" else original_get(key, *a, **kw)
        )):
            with self.assertRaises(AgentToolError):
                set_digest(args, expected_snapshot=token, owner=self.context.owner, delivery_route=self.context.delivery_route)
        self.assertEqual(digests(owner=self.context.owner)["data"]["items"], [])

    def test_new_disabled_digest_is_not_reported_as_stopping_an_existing_rule(self):
        active = {"enabled": True, "hour": 21}
        prepared = prepare_set_digest(active, owner=self.context.owner, delivery_route=self.context.delivery_route)
        _, token = prepared["preview"], prepared["snapshot"]
        existing = set_digest(active, expected_snapshot=token, owner=self.context.owner, delivery_route=self.context.delivery_route)["data"]
        disabled = {"enabled": False, "hour": 22}
        prepared = prepare_set_digest(disabled, owner=self.context.owner, delivery_route=self.context.delivery_route)
        preview, token = prepared["preview"], prepared["snapshot"]
        self.assertIn("新增", preview["summary"])
        created = set_digest(disabled, expected_snapshot=token, owner=self.context.owner, delivery_route=self.context.delivery_route)
        self.assertIn("新增", created["summary"])
        self.assertIn("当前停用", created["summary"])
        rows = digests(owner=self.context.owner)["data"]["items"]
        self.assertEqual(len(rows), 2)
        self.assertTrue(next(row for row in rows if row["rule_id"] == existing["rule_id"])["enabled"])


    def test_digest_update_without_hour_preserves_schedule_and_only_changes_requested_fields(self):
        args = {"enabled": True, "hour": 21, "minute": 17, "errors_only": True, "send_empty": True}
        prepared = prepare_set_digest(args, owner=self.context.owner, delivery_route=self.context.delivery_route)
        _, token = prepared["preview"], prepared["snapshot"]
        saved = set_digest(args, expected_snapshot=token, owner=self.context.owner, delivery_route=self.context.delivery_route)
        for update, expected in (({"enabled": False}, {"hour": 21, "minute": 17}),
                                 ({"enabled": True, "minute": 35}, {"hour": 21, "minute": 35})):
            update = {"rule_id": saved["data"]["rule_id"], **update}
            prepared = prepare_set_digest(update, owner=self.context.owner, delivery_route=self.context.delivery_route)
            preview, token = prepared["preview"], prepared["snapshot"]
            self.assertEqual(preview["data"]["hour"], expected["hour"])
            saved = set_digest(update, expected_snapshot=token, owner=self.context.owner, delivery_route=self.context.delivery_route)
            self.assertEqual(saved["data"]["enabled"], update["enabled"])
            self.assertEqual(saved["data"]["settings"], {**expected, "errors_only": True, "send_empty": True})

    def test_digest_schema_and_validator_agree_on_create_and_update(self):
        from app.agent.kernel.ports.sdk_effects import automation_tools
        from app.agent.errors import ToolPipelineError
        from app.agent.schema import validate_json_schema

        tool = next(spec for spec in automation_tools() if spec.name == "automation.set_digest")
        valid = [{"enabled": True, "hour": 21}, {"enabled": True, "hour": 0, "rule_id": ""},
                 {"enabled": False, "rule_id": "existing"},
                 {"enabled": True, "rule_id": "existing", "minute": 35}]
        for payload in valid:
            with self.subTest(payload=payload):
                validate_json_schema(payload, tool.input_schema)
                normalized = digest_arguments(payload)
                self.assertEqual("hour" in normalized, "hour" in payload)
        invalid = [{"enabled": True}, {"enabled": False, "rule_id": ""},
                   {"enabled": True, "hour": None}, {"enabled": True, "rule_id": 123}]
        for payload in invalid:
            with self.subTest(payload=payload):
                with self.assertRaises(ToolPipelineError):
                    validate_json_schema(payload, tool.input_schema)
                with self.assertRaises(AgentToolError):
                    digest_arguments(payload)

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
        assert row is not None
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
        assert row is not None
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
            with self.assertRaises(MediaSubscriptionError):
                subscription_create_arguments(
                    {"provider": "tmdb", "external_id": "123", "media_type": "tv", **extra}
                )
        for args in (
            {"enabled": "yes", "hour": 21},
            {"enabled": True, "hour": True},
            {"enabled": True, "hour": 25},
        ):
            with self.assertRaises(AgentToolError):
                digest_arguments(args)



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
                assert row is not None
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
            assert row is not None
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

    def test_daily_digest_preserves_topic_when_disabled_and_routes_after_reenable(self):
        from app.repositories.agent_jobs import agent_job_owner_digest

        context = ToolContext(owner="tg:v1:123\x1f456", session_id="topic",
                              delivery_route={"chat_id": "123", "message_thread_id": 99})
        args = {"enabled": True, "hour": 21, "send_empty": True}
        with patch("app.modules.media_automation_rules.telegram_owner_route_is_currently_authorized", return_value=True):
            prepared = prepare_set_digest(args, owner=context.owner, delivery_route=context.delivery_route)
            _, token = prepared["preview"], prepared["snapshot"]
            saved = set_digest(args, expected_snapshot=token, owner=context.owner, delivery_route=context.delivery_route)
            rule_id = saved["data"]["rule_id"]
            owner = agent_job_owner_digest(context.owner)
            # 停用是控制操作；即使从默认聊天发起，也不应丢掉规则原话题。
            disabled = {"rule_id": rule_id, "enabled": False, "hour": 21}
            default_context = ToolContext(owner=context.owner, session_id="default")
            prepared = prepare_set_digest(disabled, owner=default_context.owner, delivery_route=default_context.delivery_route)
            _, token = prepared["preview"], prepared["snapshot"]
            set_digest(disabled, expected_snapshot=token, owner=default_context.owner, delivery_route=default_context.delivery_route)
            row = rules.get_rule(owner, rule_id)
            assert row is not None
            self.assertEqual(row["settings"]["notification_message_thread_id"], 99)
            enabled = {"rule_id": rule_id, "enabled": True, "hour": 21}
            prepared = prepare_set_digest(enabled, owner=context.owner, delivery_route=context.delivery_route)
            _, token = prepared["preview"], prepared["snapshot"]
            set_digest(enabled, expected_snapshot=token, owner=context.owner, delivery_route=context.delivery_route)
            row = rules.get_rule(owner, rule_id)
            assert row is not None
            with patch("app.modules.media_automation_rules.publish_notification_event", return_value=Mock(status="pending")) as publish:
                self.assertEqual(drain_automation_rules(now=datetime.fromisoformat(row["next_run_at"])), 1)
            self.assertEqual(publish.call_args.kwargs["delivery_route"], {"chat_id": "123", "message_thread_id": 99})

    def test_topic_route_rejects_other_chat_or_invalid_thread(self):
        from app.modules.media_automation_rules import notification_route_settings

        with patch("app.modules.media_automation_rules.telegram_owner_route_is_currently_authorized", return_value=True):
            for route in ({"chat_id": "999", "message_thread_id": 77},
                          {"chat_id": "123", "message_thread_id": -1},
                          {"chat_id": "123", "message_thread_id": True}):
                with self.subTest(route=route), self.assertRaises(AgentToolError):
                    notification_route_settings("tg:v1:123\x1f456", delivery_route=route)
