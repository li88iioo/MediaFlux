"""媒体消费、显式偏好、今日摘要与通知规则的 Agent 回归。"""
from __future__ import annotations

from unittest.mock import Mock, patch

from app import database as db
from app.agent.media_consumption_actions import (
    continue_watching_arguments,
    notification_rule_update_arguments,
    preferences_update_arguments,
)
from app.agent.orchestrator import (
    continue_watching_request,
    is_today_media_summary_message,
    media_preferences_request,
    media_subscription_notification_rule_request,
)
from app.agent.models import ToolResult
from app.agent.rate_limit import agent_rate_limiter
from app.agent.registry import AgentToolError
from app.agent.service import get_agent_service, reset_agent_service_for_tests
from app.clients.base import MediaItem
from app.modules.media_server_profiles import MediaServerProfile
from tests.support import IsolatedDatabaseTestCase


class MediaConsumptionAgentTests(IsolatedDatabaseTestCase):
    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM media_subscription_notification_outbox")
            conn.execute("DELETE FROM media_subscription_notification_rules")
            conn.execute("DELETE FROM media_subscription_runs")
            conn.execute("DELETE FROM media_subscriptions")
            conn.execute("DELETE FROM agent_media_preferences")
            conn.execute("DELETE FROM agent_action_history")
            conn.execute("DELETE FROM download_log")
        reset_agent_service_for_tests()
        self.sid = db.add_media_subscription(
            provider="tmdb",
            external_id="12345",
            tmdb_id="12345",
            media_type="tv",
            title="示例追更",
            original_title="PRIVATE ORIGINAL",
            year="2026",
            poster_key="/private/poster.jpg",
            action="confirm",
            download_target="guangya",
            sites=("mikan",),
            enabled=True,
        )

    def tearDown(self) -> None:
        reset_agent_service_for_tests()
        agent_rate_limiter.reset()

    def test_registry_exposes_read_and_confirmation_tools(self) -> None:
        service = get_agent_service()
        capabilities = {item["name"]: item for item in service.capabilities()["tools"]}
        for name in (
            "media.continue_watching",
            "media.preferences",
            "media.today_summary",
            "media.subscription_notification_rule",
            "media_proxy.playback_failure_summary",
        ):
            self.assertEqual(capabilities[name]["risk"], "read")
            self.assertFalse(capabilities[name]["requires_confirmation"])
        for name in (
            "media.set_preferences",
            "media.clear_preferences",
            "media.set_subscription_notification_rule",
            "media.reset_subscription_notification_rule",
        ):
            self.assertEqual(capabilities[name]["risk"], "low_write")
            self.assertTrue(capabilities[name]["requires_confirmation"])

    def test_validators_are_strict_and_bounded(self) -> None:
        self.assertEqual(
            continue_watching_arguments({}), {"server": "auto", "limit": 8}
        )
        self.assertEqual(
            preferences_update_arguments({"preferred_download_target": "both"}),
            {"preferred_download_target": "both"},
        )
        self.assertEqual(
            notification_rule_update_arguments({
                "subscription_number": 1,
                "enabled": True,
                "notify_on_missing": True,
            }),
            {"subscription_number": 1, "enabled": True, "notify_on_missing": True},
        )
        with self.assertRaises(AgentToolError):
            continue_watching_arguments({"server": "admin"})
        with self.assertRaises(AgentToolError):
            preferences_update_arguments({"quality_profile": "quality"})
        with self.assertRaises(AgentToolError):
            notification_rule_update_arguments({"subscription_number": 1})

    def test_continue_watching_requires_explicit_user_and_redacts_internal_ids(self) -> None:
        service = get_agent_service()
        profile = MediaServerProfile(
            source="configured:jellyfin",
            server_type="jellyfin",
            label="Jellyfin",
            url="http://private.local",
            credential="PRIVATE-TOKEN",
            enabled=True,
            user_id="PRIVATE-USER-ID",
        )
        client = Mock()
        client.continue_watching.return_value = [
            MediaItem(
                id="PRIVATE-ITEM-ID",
                name="第 3 集",
                type="Episode",
                web_url="http://private.local/web/index.html#!/details?id=secret",
                series_name="示例动画",
                season_number=1,
                episode_number=3,
                last_played="2026-08-23T09:30:00+08:00",
                progress=42.5,
            )
        ]
        with (
            patch(
                "app.agent.media_consumption_actions.list_configured_profiles",
                return_value=[profile],
            ),
            patch("app.agent.media_consumption_actions._client", return_value=client),
        ):
            response = service.invoke(
                "media.continue_watching",
                {"server": "auto", "limit": 8},
                owner="owner-a",
            )
        item = response["result"]["data"]["items"][0]
        self.assertEqual(item["title"], "示例动画")
        self.assertEqual(item["episode"], 3)
        self.assertEqual(item["progress"], 42.5)
        serialized = repr(response)
        for secret in (
            "PRIVATE-ITEM-ID", "PRIVATE-USER-ID", "PRIVATE-TOKEN", "private.local"
        ):
            self.assertNotIn(secret, serialized)
        client.continue_watching.assert_called_once_with("PRIVATE-USER-ID", limit=8)

        profile_without_user = MediaServerProfile(
            source="configured:jellyfin",
            server_type="jellyfin",
            label="Jellyfin",
            url="http://private.local",
            credential="PRIVATE-TOKEN",
            enabled=True,
            user_id="",
        )
        with patch(
            "app.agent.media_consumption_actions.list_configured_profiles",
            return_value=[profile_without_user],
        ):
            blocked = service.invoke(
                "media.continue_watching", {}, owner="owner-a"
            )
        self.assertEqual(blocked["result"]["status"], "precondition_failed")
        self.assertIn("不会回退", blocked["result"]["error"])

    def test_preferences_are_owner_isolated_confirmation_gated_and_clearable(self) -> None:
        service = get_agent_service()
        prepared = service.prepare(
            "media.set_preferences",
            {
                "preferred_server": "jellyfin",
                "preferred_download_target": "both",
            },
            owner="owner-a",
        )
        self.assertEqual(prepared["mode"], "confirmation_required")
        before = service.invoke("media.preferences", {}, owner="owner-a")
        self.assertFalse(before["result"]["data"]["explicit"])
        confirmed = service.confirm(
            prepared["action_plan"]["plan_id"], owner="owner-a"
        )
        self.assertEqual(confirmed["result"]["status"], "completed")

        owner_a = service.invoke("media.preferences", {}, owner="owner-a")
        owner_b = service.invoke("media.preferences", {}, owner="owner-b")
        self.assertEqual(owner_a["result"]["data"]["preferred_server"], "jellyfin")
        self.assertTrue(owner_a["result"]["data"]["explicit"])
        self.assertEqual(owner_b["result"]["data"]["preferred_server"], "any")
        self.assertFalse(owner_b["result"]["data"]["explicit"])

        clear = service.prepare("media.clear_preferences", {}, owner="owner-a")
        service.confirm(clear["action_plan"]["plan_id"], owner="owner-a")
        cleared = service.invoke("media.preferences", {}, owner="owner-a")
        self.assertFalse(cleared["result"]["data"]["explicit"])
        self.assertEqual(cleared["result"]["data"]["preferred_download_target"], "guangya")

    def test_preference_confirmation_detects_stale_revision(self) -> None:
        service = get_agent_service()
        prepared = service.prepare(
            "media.set_preferences", {"preferred_server": "jellyfin"}, owner="owner-a"
        )
        from app.agent.action_history import action_history_owner_digest
        from app.repositories.media_experience import set_media_preferences

        changed = set_media_preferences(
            action_history_owner_digest("owner-a"),
            expected_revision=0,
            updates={"preferred_server": "emby"},
        )
        self.assertIsNotNone(changed)
        with self.assertRaises(AgentToolError) as captured:
            service.confirm(
                prepared["action_plan"]["plan_id"], owner="owner-a"
            )
        self.assertEqual(captured.exception.code, "confirmation_stale")

    def test_notification_rule_is_revisioned_and_resettable(self) -> None:
        service = get_agent_service()
        defaults = service.invoke(
            "media.subscription_notification_rule",
            {"subscription_number": self.sid},
            owner="owner-a",
        )
        self.assertFalse(defaults["result"]["data"]["enabled"])
        self.assertTrue(defaults["result"]["data"]["notify_on_missing"])
        self.assertFalse(defaults["result"]["data"]["explicit"])

        prepared = service.prepare(
            "media.set_subscription_notification_rule",
            {
                "subscription_number": self.sid,
                "enabled": True,
                "notify_on_satisfied": True,
            },
            owner="owner-a",
        )
        service.confirm(prepared["action_plan"]["plan_id"], owner="owner-a")
        explicit = service.invoke(
            "media.subscription_notification_rule",
            {"subscription_number": self.sid},
            owner="owner-b",
        )
        self.assertTrue(explicit["result"]["data"]["enabled"])
        self.assertTrue(explicit["result"]["data"]["notify_on_satisfied"])
        self.assertTrue(explicit["result"]["data"]["explicit"])

        reset = service.prepare(
            "media.reset_subscription_notification_rule",
            {"subscription_number": self.sid},
            owner="owner-a",
        )
        service.confirm(reset["action_plan"]["plan_id"], owner="owner-a")
        restored = service.invoke(
            "media.subscription_notification_rule",
            {"subscription_number": self.sid},
            owner="owner-a",
        )
        self.assertFalse(restored["result"]["data"]["enabled"])
        self.assertFalse(restored["result"]["data"]["explicit"])

    def test_today_summary_prioritizes_bounded_safe_content_events(self) -> None:
        stamp = db.now()
        revision = int(db.get_media_subscription(self.sid)["revision"])
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO media_subscription_runs("
                "subscription_id,trigger_type,subscription_revision,status,summary,"
                "payload_json,error,started_at,finished_at) VALUES(?, 'manual', ?, "
                "'missing', '', '{}', '', ?, ?)",
                (self.sid, revision, stamp, stamp),
            )
            conn.execute(
                "INSERT INTO download_log(source,title,status,created_at,updated_at,completed_at) "
                "VALUES('qb', ?, 'success', ?, ?, ?)",
                ("安全标题", stamp, stamp, stamp),
            )
            conn.execute(
                "INSERT INTO download_log(source,title,status,created_at,updated_at,completed_at) "
                "VALUES('qb', ?, 'success', ?, ?, ?)",
                ("/private/library/SECRET.mkv", stamp, stamp, stamp),
            )
        response = get_agent_service().invoke("media.today_summary", {}, owner="owner-a")
        data = response["result"]["data"]
        self.assertEqual(data["event_count"], 3)
        self.assertEqual(data["subscription_runs"]["missing"], 1)
        self.assertEqual(data["downloads"]["success"], 2)
        self.assertIn("安全标题", data["content_titles"])
        self.assertNotIn("SECRET", repr(response))
        self.assertLessEqual(len(data["content_titles"]), 8)

    def test_explicit_download_target_preference_is_used_for_recent_resource(self) -> None:
        service = get_agent_service()
        prepared = service.prepare(
            "media.set_preferences",
            {"preferred_download_target": "qb"},
            owner="owner-a",
        )
        service.confirm(prepared["action_plan"]["plan_id"], owner="owner-a")
        service.recent_resource_store.capture(
            owner="owner-a",
            result=ToolResult(
                True,
                "success",
                "找到资源",
                data={
                    "items": [{
                        "result_id": "resource-result-0001",
                        "title": "安全候选",
                        "site_id": "mikan",
                        "site_name": "Mikan",
                        "size_text": "1 GiB",
                        "download_state": "ready",
                        "download_kinds": ["magnet"],
                    }]
                },
            ),
        )
        with patch.object(
            service,
            "prepare",
            return_value={"mode": "confirmation_required"},
        ) as prepare_mock:
            response = service._continue_recent_resource_submit(
                {"position": 1, "target": None}, owner="owner-a"
            )
        self.assertEqual(response["mode"], "confirmation_required")
        self.assertEqual(
            prepare_mock.call_args.args[:2],
            (
                "indexer.submit_candidate",
                {"position": 1, "target": "qb"},
            ),
        )

    def test_natural_language_parsers_bind_exact_safe_tools(self) -> None:
        self.assertEqual(
            continue_watching_request("查看 Jellyfin 继续观看前 5 个"),
            {"server": "jellyfin", "limit": 5},
        )
        self.assertEqual(
            media_preferences_request("以后默认下载到光鸭"),
            (
                "media.set_preferences",
                {"preferred_download_target": "guangya"},
            ),
        )
        self.assertIsNone(
            media_preferences_request("媒体偏好改成国语，默认下载到光鸭")
        )
        self.assertEqual(
            media_subscription_notification_rule_request(
                f"开启媒体订阅 {self.sid} 的缺集通知"
            ),
            (
                "media.set_subscription_notification_rule",
                {
                    "subscription_number": self.sid,
                    "notify_on_missing": True,
                    "enabled": True,
                },
            ),
        )
        self.assertTrue(is_today_media_summary_message("今天下载和入库了什么"))
        self.assertFalse(is_today_media_summary_message("今天新番播什么"))

    def test_natural_queries_route_without_guessing(self) -> None:
        service = get_agent_service()
        preferences = service.query("查看我的媒体偏好", owner="owner-a", present=False)
        self.assertEqual(preferences["tool_call"]["name"], "media.preferences")
        rule = service.query(
            f"查看媒体订阅 {self.sid} 的通知规则", owner="owner-a", present=False
        )
        self.assertEqual(
            rule["tool_call"]["name"], "media.subscription_notification_rule"
        )
        prepared = service.query(
            f"开启媒体订阅 {self.sid} 的缺集通知",
            owner="owner-a",
            present=False,
        )
        self.assertEqual(prepared["mode"], "confirmation_required")
        self.assertEqual(
            prepared["tool_call"]["name"],
            "media.set_subscription_notification_rule",
        )
