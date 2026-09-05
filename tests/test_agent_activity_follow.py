"""跟踪走同一持久规则/通知 outbox，确认前不写，取消/重启后不误发。"""

from datetime import datetime, timedelta
from unittest.mock import Mock, patch

from app import config
from app import database as db
from app.agent.activity_follow_actions import (
    follow_arguments,
    follow_confirmed,
    list_follows,
    prepare_follow,
    prepare_stop,
    stop_confirmed,
)
from app.agent.errors import AgentToolError
from app.agent.models import ToolContext
from app.agent.owner_routes import web_kernel_owner
from app.modules.activity_follow_notifications import deliver_activity_follow
from app.modules.media_automation_rules import (
    drain_automation_rules,
    register_rule_handler,
)
from tests.support import IsolatedDatabaseTestCase


class ActivityFollowTests(IsolatedDatabaseTestCase):
    def setUp(self):
        enabled = patch(
            "app.modules.media_automation_rules.is_agent_enabled", return_value=True
        )
        enabled.start()
        self.addCleanup(enabled.stop)
        with db.get_conn() as conn:
            conn.execute("DELETE FROM media_automation_rules")
            conn.execute("DELETE FROM download_request_keys")
            conn.execute("DELETE FROM download_log")
            conn.execute("DELETE FROM download_requests")
        self.context = ToolContext(
            owner=web_kernel_owner("follow-test"), session_id="one"
        )
        identifier, _ = db.create_download_request(
            "follow-request", "magnet", title="测试剧集"
        )
        db.update_download_request(
            identifier, status="downloading", gy_status="downloading"
        )
        self.identifier = identifier
        self.arguments = {
            "activity_selection": {"items": [{"kind": "download", "id": identifier}]},
            "hours": 24,
        }
        get = config.get
        patched = patch(
            "app.modules.media_automation_rules.config.get",
            side_effect=lambda key, *args, **kw: (
                "123" if key == "TG_CHAT_ID" else get(key, *args, **kw)
            ),
        )
        patched.start()
        self.addCleanup(patched.stop)
        register_rule_handler("activity_follow", deliver_activity_follow)

    def save(self):
        preview, token = prepare_follow(self.arguments, self.context)
        self.assertEqual(preview.status, "confirmation_required")
        return follow_confirmed(self.arguments, token, self.context)

    def test_preparation_is_readonly_and_confirm_does_not_complete_download(self):
        _, token = prepare_follow(self.arguments, self.context)
        self.assertEqual(list_follows({}, self.context).data["items"], [])
        result = follow_confirmed(self.arguments, token, self.context)
        self.assertTrue(result.data["enabled"])
        self.assertIn("不表示原任务已完成", result.summary)
        with db.get_conn() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT status FROM download_requests WHERE id=?",
                    (self.identifier,),
                ).fetchone()[0],
                "downloading",
            )
        with self.assertRaises(AgentToolError):
            follow_confirmed(self.arguments, token, self.context)

    def test_owner_session_duration_and_route_changes_reject(self):
        _, token = prepare_follow(self.arguments, self.context)
        contexts = [
            ToolContext(owner=web_kernel_owner("other"), session_id="one"),
            ToolContext(owner=self.context.owner, session_id="two"),
        ]
        for context in contexts:
            with self.assertRaises(AgentToolError):
                follow_confirmed(self.arguments, token, context)
        with self.assertRaises(AgentToolError):
            follow_confirmed({**self.arguments, "hours": 2}, token, self.context)
        with (
            patch("app.modules.media_automation_rules.config.get", return_value="456"),
            self.assertRaises(AgentToolError),
        ):
            follow_confirmed(self.arguments, token, self.context)

    def test_read_running_sends_nothing_then_terminal_notifies_once(self):
        saved = self.save()
        now = datetime.now().astimezone() + timedelta(seconds=1)
        with patch(
            "app.modules.media_automation_rules.publish_notification_event",
            return_value=Mock(status="queued"),
        ) as send:
            self.assertEqual(drain_automation_rules(now=now), 1)
            send.assert_not_called()
            db.update_download_request(
                self.identifier, status="completed", gy_status="completed"
            )
            self.assertEqual(drain_automation_rules(now=now + timedelta(minutes=6)), 1)
            send.assert_called_once()
            event = send.call_args.args[1]
            self.assertIn("不等于已入库", event.lines[0])
            self.assertIn("未建立媒体库可见性复核", str(event.fields))
            self.assertEqual(drain_automation_rules(now=now + timedelta(minutes=12)), 0)
            self.assertFalse(list_follows({}, self.context).data["items"][0]["enabled"])
            self.assertEqual(send.call_args.kwargs["chat_id"], "123")
        self.assertTrue(saved.ok)

    def test_stop_owner_scope_does_not_cancel_request(self):
        saved = self.save()
        args = {"rule_id": saved.data["rule_id"]}
        with self.assertRaises(AgentToolError):
            prepare_stop(args, ToolContext(owner=web_kernel_owner("other")))
        _, token = prepare_stop(args, self.context)
        self.assertTrue(stop_confirmed(args, token, self.context).ok)
        with self.assertRaises(AgentToolError):
            stop_confirmed(args, token, self.context)
        self.assertEqual(
            drain_automation_rules(
                now=datetime.now().astimezone() + timedelta(minutes=6)
            ),
            0,
        )

    def test_expiry_reports_unknown_not_completion_and_stops(self):
        self.save()
        with patch(
            "app.modules.media_automation_rules.publish_notification_event",
            return_value=Mock(status="queued"),
        ) as send:
            drain_automation_rules(
                now=datetime.now().astimezone() + timedelta(hours=25)
            )
            self.assertIn("尚未确认结束", str(send.call_args.args[1].fields))
        self.assertFalse(list_follows({}, self.context).data["items"][0]["enabled"])

    def test_failure_notification_once_and_no_retained_provider_secrets(self):
        self.save()
        db.update_download_request(
            self.identifier,
            status="failed",
            gy_status="failed",
            error="token=secret /private/file",
        )
        with patch(
            "app.modules.media_automation_rules.publish_notification_event",
            return_value=Mock(status="queued"),
        ) as send:
            drain_automation_rules(
                now=datetime.now().astimezone() + timedelta(seconds=1)
            )
            self.assertNotIn("secret", str(send.call_args.args))
            self.assertIn("关注", send.call_args.args[1].title)
        self.assertFalse(list_follows({}, self.context).data["items"][0]["enabled"])

    def test_invalid_duration_and_injected_route_rejected(self):
        for args in (
            {"activity_selection_ref": "ref_" + "a" * 24, "hours": True},
            {"activity_selection_ref": "ref_" + "a" * 24, "chat_id": "321"},
        ):
            with self.assertRaises(AgentToolError):
                follow_arguments(args)

    def test_agent_disabled_does_not_claim_or_send_web_owned_rules(self):
        self.save()
        with (
            patch(
                "app.modules.media_automation_rules.is_agent_enabled",
                return_value=False,
            ),
            patch(
                "app.modules.media_automation_rules.publish_notification_event"
            ) as send,
        ):
            self.assertEqual(
                drain_automation_rules(
                    now=datetime.now().astimezone() + timedelta(minutes=6)
                ),
                0,
            )
            send.assert_not_called()
        self.assertTrue(list_follows({}, self.context).data["items"][0]["enabled"])

    def test_disable_during_read_prevents_late_outbox_admission(self):
        self.save()
        from app.modules.media_automation_rules import RuleDelivery
        from app.notifier import NotificationEvent

        with (
            patch(
                "app.modules.media_automation_rules.is_agent_enabled",
                side_effect=[True, False],
            ),
            patch(
                "app.modules.activity_follow_notifications.deliver_activity_follow",
                return_value=RuleDelivery(
                    NotificationEvent("任务结束"),
                    "one",
                    datetime.now().astimezone().isoformat(),
                    terminal=True,
                ),
            ),
            patch(
                "app.modules.media_automation_rules.publish_notification_event"
            ) as send,
        ):
            self.assertEqual(
                drain_automation_rules(
                    now=datetime.now().astimezone() + timedelta(seconds=1)
                ),
                0,
            )
            send.assert_not_called()
        self.assertTrue(list_follows({}, self.context).data["items"][0]["enabled"])
